# telegram-mtproto-ai — Codex 项目指令

> 本仓库是多平台 AI 客服的主骨架。Codex 在本 cwd 启动时自动加载本文件。
> **边界声明**见 [`docs/PROJECT_SCOPE.md`](docs/PROJECT_SCOPE.md)（权威文档）。

## 仓库一句话

`main.py` 启 FastAPI，内嵌：contacts/handoff 子系统 + Telegram/LINE/Messenger 三端 RPA runner + skill_manager / KB / 回复生成 / 语言守卫 + Web 后台 + observability。

## Codex 在本 repo 工作时的约定

### 回归命令

**全量**（pytest.ini `asyncio_mode=auto` + pytest-asyncio plugin，0 ignore）：
```bash
python -m pytest tests/ -n auto -q
```
预期：全绿，0 fail，CI ~50 秒（baseline 266 → 4x+ 当前规模；不存具体数字，每次合 PR 会增加，按 `git log` 看实际）。

> ⚠️ 本机若有常驻服务（app/RPA runner）在跑，`-n auto` 会与之争 CPU 把全量拖到数分钟，
> 且**无超时时任一 worker 卡住会无限等**（曾出现「跑 50 分钟不结束」）。本机跑全量建议固定带超时兜底
> （挂起会被点名而非无限等，已装 `pytest-timeout`）：
> ```bash
> python -m pytest tests/ -n auto -q --timeout=90 --timeout-method=thread
> ```
>
> 防陈旧字节码 flaky（曾偶发 `test_*_event_alias`）：用 `scripts/regression.ps1`（Win）/
> `scripts/regression.sh`（posix）——跑前清 `src/tests` 的 `__pycache__` 并 `PYTHONDONTWRITEBYTECODE=1`，
> 含上述超时兜底；可透传 pytest 参数（如 `scripts\regression.ps1 tests\test_x.py`）。

**仅 contacts/handoff 主线**（快速回归 — P24-D 起默认开 `-n auto` 并行，~1.9x 加速）：
```bash
python -m pytest tests/test_contacts_*.py tests/test_gateway_*.py \
  tests/test_account_limiter.py tests/test_handoff_readiness.py \
  tests/test_intimacy_engine.py tests/test_reactivation_scheduler.py \
  tests/test_handoff_*.py tests/test_cap_alert.py \
  tests/test_rpa_contact_hooks_wireup.py tests/test_contacts_runner_bridge.py \
  tests/test_rpa_shared.py tests/test_rpa_shared_yaml.py \
  tests/test_intent_tags_rate_limit.py \
  tests/test_audit_throttle.py tests/test_intent_tags_watcher.py \
  -n auto -q --tb=line
```
（去掉 `-n auto` 改单线程更利于看错误 trace；CI 默认应保留并行）
预期：全绿（contacts/handoff 主线 + intent_tags admin 闭环，含 runner→真 hooks→store bridge，
以及 P14-P26 跨平台 intent_tags 字典编辑栈：
write/diff/restore/backups/rate-limit/metrics/audit-throttle/watchdog-autoreload）。

**桌面客服「受控出站 / 人审介入」主线**（P0–P7 闭环：桌面启动档 + 注入健康看板 + 选择器热修 +
受控出站 hold/拦截/改写/放行 + AI 重写 + 纠正样本三元组/导出 + SLA 提醒 + 失误聚类）：
```bash
python -m pytest tests/test_desktop_*.py -q --tb=line
```
预期：全绿（出站队列状态机 + 人审介入 + 纠正样本/JSONL 导出 + SLA + 拦截聚类，
含 boot-gate / selectors / inject-health / 路由契约）。
桌面壳前端纯函数（Node 直跑，无框架）：
```bash
cd desktop && npm test
```
预期：全绿（health-panel 看板模型 / 出站行 / 待审 FIFO / 拦截 chips / SLA / fingerprint / launcher 等）。

**前端「哑按钮」门禁**（内联 `on*="fn()"` 引用的函数必须①有定义 ②全局作用域可达；防 `setMode`/`saveConfig`
那类「定义了但在 IIFE 内没挂 window」→ 点了抛 ReferenceError 静默无反应）+ **重复 DOM id 门禁**
（同页两个 `id="x"` → getElementById 只命中第一个、第二个元素静默失效）+ **孤儿 DOM 引用门禁**
（`getElementById('x').prop` 直接解引用但 `id="x"` 全站不存在 → null.prop 必崩）+ **动态点属性拼接门禁**
（字符串里 `X.name'+var` 拼点属性名，var 含连字符时被当减法 → ReferenceError）：
```bash
python -m pytest tests/test_inbox_inline_handlers_exported.py tests/test_rpa_inline_handlers_exposed.py tests/test_template_unique_ids.py tests/test_template_orphan_refs.py tests/test_template_dynamic_dot_access.py -q --tb=line
```
预期：全绿。扫描/作用域分析共享核心在 `tests/_inline_handler_scan.py`——会**跳过字符串/模板字面量/注释/正则**
的掩码器算括号深度，可靠区分「IIFE 内定义（不可达除非挂 window）」vs「顶层全局定义（可达）」，任意架构零假阳性；
**已扩到 `src/web/templates/**.html` 全站模板**（glob 全扫；子模板 `{% extends %}`/`{% include %}` 的跨文件全局
经 `ambient_globals`＝base+`_*.html` partial 汇入防误报）。首轮全扫已修 `agent_perf`/`workspace_dashboard`/`draft_review`
共 24 个 IIFE 内漏挂 window 的哑按钮；`personas` 的「导出/导入 JSON」按钮引用**根本不存在**的
`exportProfiles`/`importProfiles`（遗留重复卡，真面板是 `#import-panel`）已直接删除。`_PENDING_ORPHANS`（记录
「引用了未定义函数」这类需产品决策的真 bug，CI 保绿+债务可见）当前为空；`test_pending_orphans_are_still_broken` 防其过期。运行时另有兜底守卫（`unified_inbox` 内 `_wireDeadClickGuard`
+ `_rpa_shared_scripts.html` 覆盖 4 个 RPA 页）捕获 ReferenceError 弹红条（附函数名便于上报），
补静态门禁扫不到的「运行时才由 innerHTML 拼出的 handler」盲区。`referenced()` 会剔除生成期字符串拼接段
`'+helper()+'`（如 `bodyId`/`esc` 只在拼 HTML 时求值、运行时 handler 不调用），只抓真正运行时执行的调用，避免逼着无谓暴露。
**该运行时守卫已可观测化**：捕获后除弹红条，还 `navigator.sendBeacon` 到 `POST /api/telemetry/frontend-error`
（任意登录用户可写，只送消毒后的 `{page, fn, type}`——绝不送原文/查询串/堆栈），后端 `src/web/frontend_error_stats.py`
（进程级单例，风格对齐 `outbound_translation_stats`，distinct key 有上限防刷量撑爆）按 page/fn/type 累计，
经 `dump()`→`/api/workspace/metrics.frontend_errors`、`dump_prom()`→Prometheus（`frontend_errors_total` +
`..._by_{page,fn,type}_total`）观测「哪页哪函数点崩、多频」，闭合「测不到→线上也能被发现」。
ops-overview 新增「🖱️ 前端哑按钮错误」卡（`ov2_s_fe`/`ov2_js_fe_*` 键，中英齐备）展示总数/按类型 + 按页/按函数
Top8（无错=显示健康空态）。门禁 `tests/test_frontend_error_stats.py`（计数/消毒/上限/端到端 beacon→metrics）。
重复 id 门禁（`tests/test_template_unique_ids.py`）只看静态字面 id（跳过 `<script>`/HTML+Jinja 注释/`{{}}`{% %}`/拼接 id）；
已修 `dashboard`(bm-quality 串味)/`knowledge`(批量翻译按钮)/`personas`(遗留导入卡去重)/`base`(setBadge 改
querySelectorAll 同时刷桌面+移动两套导航 badge)/`whatsapp`(「对话」pane 的 P7-A 内联检索与「运维」pane 的
P11-B 共享组件检索原共用 `wa-hist-q/results`、两 pane 同在 DOM 撞车 → P11-B 换独立 `wa-ops-hist-*`，两套各自工作)；
`_ACCEPTED_DUP_IDS`（响应式镜像/互斥 Jinja 分支=假阳性）附原因登记，`_PENDING_DUP_IDS`（真 bug 待决策）当前为空，
`test_dup_id_allowlist_not_stale` 防两表过期。
孤儿引用门禁（`tests/test_template_orphan_refs.py`）**只守高置信必崩的窄不变量**——`getElementById('x').prop`/`$('x').prop`
（结果立即解引用）而 `id="x"` 全站（含 `<script>` innerHTML 生成 / `el.id=` / `setAttribute` / base+partial 跨文件）
都没有；**刻意放过防御式引用**（`?.`/`|| fallback`/`if(!p)return`，那些容忍缺失不崩，宽口径会假阳）。已修
`line_rpa`（`lr-kpi-*-foot` 直接赋值 null → 被 try 吞成 ok%/avg/1h KPI 静默不更新；改 null 安全）；
personas 的 `previewTTS` 死代码（从不被调用、引用不存在的 `vp-*`，真编辑器用 `pe-vp-*`）已删除、其 5 个
仅此处用的 `psn_js_097..101` i18n 键一并回收，门禁强度恢复。`_PENDING_ORPHAN_REFS` 现仅剩 unified_inbox
未落地的声纹登记内联面板 `ve-*`（一整套 window 暴露 + `inbox.voice.*`，且已有出货副驾组件 `cp-voice.js` 走
同源 API；「落地内联面板 or 判定被取代后移除」属产品决策，如实追踪），`test_pending_orphan_refs_still_orphan` 防过期。
动态点属性拼接门禁（`tests/test_template_dynamic_dot_access.py`）抓「字符串里点访问+拼接扩展标识符」（`X.name'+var`
这种把代码当串拼、按变量拼点属性名的 code-gen 陷阱；普通手写 `a.b` 不会紧跟 `'+` 故不误伤）。已修
`_rpa_shared_scripts.html::initSearch`：原按 `inputId` 拼 `(window.__rpaPick_'+inputId+')(...)` inline onclick，
三个 RPA 调用方 inputId 全含连字符（`wa-ops-hist-q`/`lr-hist-q`/`mr-hist-q`）→ `window.__rpaPick_wa-ops-hist-q`
被解析成减法 → 点搜索结果开会话抽屉在 LINE/Messenger/WhatsApp 三页全坏（且触发 dead-click 红条兜底）；
改**事件委托 + `data-rpa-ck` 属性**（结果容器一次绑定处理所有动态行，彻底去掉「每输入框全局函数+dot 访问」脆弱模式）。
`_ALLOWLIST`（良性命中，如字面量以 `.ext` 结尾再拼变量的文件名串）当前为空，`test_allowlist_not_stale` 防过期。

**陪伴能力「分阶段开启」主线**（看→校→开→观测→纠偏 闭环；纯函数 core 在 `src/companion/`，
路由 `src/web/routes/companion_capability_routes.py` 挂 `/api/companion/capabilities*`，
看板卡片在 `rpa_overview.html`，配置体检接进 `ops-overview`）：
```bash
python -m pytest tests/test_companion_capability_status.py \
 tests/test_companion_delivery_calibration.py tests/test_companion_capability_toggle.py \
 tests/test_companion_capability_presets.py tests/test_companion_readiness_signals.py \
 tests/test_companion_capability_advisor.py tests/test_companion_proactive.py \
 tests/test_outbound_translate.py tests/test_autosend_worker_translate.py \
 tests/test_ops_overview.py tests/test_admin_route_inventory.py -q --tb=line
```
预期：全绿（能力就绪度看板 + 真发开闸校准 + 带护栏 toggle/overlay 写入 + 一键预设档/快照回滚 +
决策信号 + 档×信号联动建议/一致性体检 + 出站自动翻译闭环 + ops-overview 配置健康灯 + 路由契约）。
关键不变量：真发主开关 `inbox.l2_autosend.deliver` 双重 opt-in（worker on + auto_ai 会话），
所有开关写经 `config.local.yaml` overlay（保住主配置注释），单切/预设/回滚/一键修复均过同一护栏；
出站自动翻译（`inbox.l2_autosend.translate.enabled`）覆盖 **L2 autosend + 主动触达(care/reactivation
经 deferred 队列)**，投递前把消息译成会话客户语言；**自带源语言检测护栏**——文本已是客户语言
（陪伴回复/reactivation 本就按客户语言生成）即跳过不译，防 garble；任何异常/不可译/译文==原文
一律回落发原文，**绝不阻塞投递**。

**每人设「相册/媒体」主线**（图/视频备货 + 触发词自动发；DB 注册表 `src/companion/persona_media_store.py`
＝`config/persona_media.db`，纯函数匹配器 `persona_media.py`，探针 `media_probe.py`，路由
`persona_media_routes.py` 挂 `/api/personas/{pid}/media*`，UI 在 `personas.html` 相册面板，
迁移 CLI `scripts/import_persona_albums.py`。详见 `docs/PERSONA_MEDIA_ALBUMS.md`）：
```bash
python -m pytest tests/test_persona_media.py tests/test_persona_media_routes.py \
 tests/test_persona_media_import.py tests/test_media_probe.py \
 tests/test_selfie_wiring.py tests/test_image_autosend.py -q --tb=line
```
预期：全绿。关键不变量：命中相册**优先于**AI 现场出图（两条链 Stage 0：autosend `run_autosend_image` +
skill_manager `_handle_persona_media_request`）；关键词池独立于自拍/物体意图、通用池仅泛化要图时放开、
`min_bond_level` 关系闸门、加权轮播+会话内避重；护栏＝扩展名白名单/体积(图10M/视频50M)/视频时长(3min,
仅 ffprobe 可探时拦)/sha256 去重/路径消毒/viewer 只读/审计(`pmedia_*`)；探针（ffprobe 时长宽高、ffmpeg
封面、PIL 图宽高）全软失败不阻塞上传；多语配文 `caption_i18n` 随会话语种取文；观测经
`/api/workspace/metrics.persona_media` + Prometheus `ws_persona_media_*` + ops-overview「🖼️ 人设相册」卡。
总开关沿用 `companion.selfie.enabled`。

**出站媒体承诺守卫 + 文图一致性主线**（2026-07-13/14：修「嘴上说发照片实际没发」与「发图后失忆」）：
```bash
python -m pytest tests/test_outbound_promise_guard.py tests/test_selfie_wiring.py \
 tests/test_image_autosend.py tests/test_autosend_helpers.py \
 tests/test_inbox_draft_engine.py tests/test_scene_state.py -q --tb=line
```
**Phase18 增量**（2026-07-14 凌晨，场景状态化+已发媒体日志+异步兑现+多语守卫）：
① **场景单一事实源**（`resolve_current_scene`/`scene_chat_note`，`selfie.scene_in_chat` 默认开）：
  「AI 此刻在哪」升格为会话状态——聊天 prompt（【你此刻的状态】块）与生图（A 线 Stage A/
  B 线 run_autosend_image/异步兑现）同源取值（同日同时段恒定），从因果层消灭"文本说上班、
  图发海边"打脸，零 LLM 调用。客户显式点名场景（`extract_requested_scene` 词表：海边/办公室/
  健身房…12 类）→ directive.scene 覆盖轮换；「跟上次一样的」（`wants_same_scene`）→ 取媒体日志
  上次场景。配文 LLM 拿到照片实际场景（`build_photo_caption_instruction(scene=)`），图-文-配文三同源。
② **已发媒体日志**（`_record_media_sent` → `user_context._media_sent_log`，bounded 5，随
  ContextStore 持久化）：A 线各媒体路径（Stage 0/A/B/C + photo_directive + 异步兑现）落
  {ts,note,scene}；prompt 出「你最近发过的照片」事实块——"我没发过照片"式失忆抵赖不再发生。
  刻意**不写 episodic memory**（系统行为混进"用户事实"会污染记忆语义，Phase8 教训）。
  B 线限制：autosend worker 无 user_context，靠 inbox `[图片] 配文` 叙事（结构化日志属 P3）。
③ **A 线异步兑现**（`media_promise_guard.async_fulfill` 基线关/本机 overlay 开）：LLM 承诺发图
  且**同步预检**全过（selfie 开+真出图后端+decide_selfie allow+预算+图文双通道+无 in-flight）
  → 保留承诺原文 + 后台延迟 4-9s 真拍真发（`_photo_directive_selfie` 全套管线复用，场景与聊天
  注入同源）；失败自动补语言对齐台阶文本（"手机抽风…改天补"，`promise_fail` 模板 zh/en）——
  要么图到、要么圆场，绝不静默装死。photo_directive 刚试败（`_photo_attempt_failed`）不重试防复烧。
④ **守卫多语扩展**：承诺检测 +ja/ko/es/fr/pt 宣告形（写真送るね/사진 보내줄게/te mando una foto…），
  排除面同步扩多语否认/远期/offer 疑问；撤回兜底话术 zh/ja/ko/en 按文字系统取（假名先于汉字判 ja）。
  新观测键：`promise_fulfill_scheduled/fulfilled_async/fulfill_failed`。门禁 `tests/test_scene_state.py`。
**Phase20 增量**（2026-07-14 拂晓，B 线日志合流+行程线+图文一致性验收网）：
① **B 线媒体日志合流**：deliver 与 draft 同属 worker 线程串行流程 → `autosend_image`
  发图成功经 `run_autosend_image(on_sent=)` 回调直写 A 线同款 `_media_sent_log`
  （零新表零迁移，A/B 数据合流）；「跟上次一样的」B 线版经 `requested_scene` 参数
  （wants_same_scene → 日志上次场景 → directive.scene，优先于轮换、低于显式点名/LLM 指令）。
  相册现成图 scene 记空串（不误标）；on_sent 异常绝不影响已完成的发送。
② **行程线**（`build_day_itinerary` + `scene_chat_note(itinerary=)`，`selfie.scene_itinerary`
  默认开随 scene_in_chat 总闸）：场景从「点」到「线」——同一确定性函数按今天四时段
  （上午/下午/傍晚/深夜，代表小时与 pick_scene_hint bucket 一一对应）各取一景，注入
  「今天动线」行并标注（现在）；LLM 可自然引用"早上去过哪（过去时）/晚点打算干嘛
  （将来时）"，跨时段叙事连贯。各桶独立过 Phase19 时段冲突过滤；零 LLM 零存储。
  已知边界：proactive 话题贴合反选的场景可能与动线不同（真人临时改主意语义，可接受）。
③ **图文一致性验收网**（`src/eval/media_consistency_eval.py`，纯函数常驻门禁）：四类
  硬违规确定性校验——附图否认（"等我去拍"+photo_sent）/无图称已发（"自拍来啦"）/
  强断言他类场景（"我在健身房"+海边图）/场景时间词与发送时刻硬冲突（Phase19 词表
  复用）；金标=实录事故正例+易误伤反例双面（过去指涉/愿望句/home-bedroom 互容放行）。
  生成侧五层防线任何一层回归漂移，这里先红。CLI `python -m scripts.run_eval
  --media-consistency [--json]`；门禁 `tests/test_media_consistency_eval.py`（含篡改金标
  必 FAIL 的探测器自证）。路由清单债（face-ref×4+proactive/status）已补登，全量恢复全绿。
预期：全绿。根因＝发图判定看**客户入站关键词**（`plan_autosend_image`/`detect_selfie_request`），
LLM 出站文本却可能自己承诺「等我拍一张给你」，两边从不核对（实录：客户质问"你快拍啊是不是骗我"）。
五层修复（`companion.media_promise_guard` 默认开，与 persona_guard/危机兜底同族的出站正确性守卫）：
① **兑现优先**（B 线 `_autosend_deliver`）：文本承诺发图而常规图链没发 →
  `autosend_image(assume_intent="selfie")` 强制自拍链真发一张（相册通用池/生成；预算/关系闸门照常），
  成功=图替文投递；仍发不出 → **撤回**（LLM 重写去承诺 → 正则句级剥离 → 语言对齐兜底话术，
  `_depromise_autosend_text`），并置空 original_text 防语音分支克隆声念出谎话；语音承诺同理
  （语音分支未发成 → 文本剥离）。A 线 `_apply_media_promise_guard`（5c2）同步纯剥离（零 LLM 延迟）。
② **offer-accept 桥**（`offer_accepted`，`companion.selfie.offer_accept_bridge` 默认开）：上一轮 AI 问
  「要不要看照片」、本条客户短肯定（"好呀/要/嗯嗯"，`detect_selfie_request` 抓不住）→ 视同要图请求
  真发图——offer 不再变空头支票。A 线 Stage 0/A + B 线 `run_autosend_image` 三处同判。
③ **拟稿 hint**（draft 3b2 + `_media_coherence_hint` → `_build_context_prompt`「发图协同」块）：
  对方在要图时告知草稿 LLM「本文本只是图片失败时的兜底，禁写『等我去拍/照片来了』」——
  草稿天生自洽（图成功时草稿被丢弃、caption 由 llm_caption 另生成）；A 线 Stage B 各失败口
  同 key 注入「这轮发不出，别承诺」。无 hint 时 selfie 启用部署注入常驻「媒体能力边界」声明
  （`capability_hint` 可关）。
④ **媒体轮回写**（`_record_stage_turn` + `_stage_media_note`）：A 线 Stage 0/A/B/C 短路直发后把
  (用户消息, "[图片] 配文"/搪塞文字) 写进 last_message/last_reply——修「发图后失忆」（用户："这张照片
  真好看" → AI："什么照片？"）；复用既有「下轮补录进 _conversation_history」机制，媒体轮进上下文窗口。
⑤ **谎言 bug 修复 + 文案语言对齐**：A 线出图成功但发送失败不再把「这是刚拍的，给你看～」配文当文字
  发出（改自洽搪塞 + `record_image_fallback("a_line_send_failed")`）；免费额度只在**真送达**时消耗；
  Stage 短路搪塞/兜底/配文全套 zh/en 双语（`selfie_stage_text`，按会话语言取——英文会话不再蹦中文）。
观测：`metrics_snapshot()` 增 `promise_detected/fulfilled/retracted/offer_accept`（autosend-status image 段）。

**外部 worker 会话健康 + Messenger 受控降级主线**（2026-07：网页链路不稳的止血与自愈闭环）：
```bash
python -m pytest tests/test_messenger_send_semantics.py tests/test_platform_session_health.py \
 tests/test_platform_session_selfheal.py tests/test_auto_draft_platform_modes.py \
 tests/test_alert_delivery_e2e.py tests/test_admin_route_inventory.py -q --tb=line
```
预期：全绿。链路：messenger-web / whatsapp-baileys(Node) 在登录/掉线/放弃自愈时 POST
`/api/internal/protocol/session-status` → `src/integrations/platform_session_health.py`
（进程级登记表）→ 转移告警（EventBus `platform_session_alert`，订阅别名 `platform_session`，
事件带 `rate_key=platform:acct` 防多账号挤限流窗）→ worker send/send_media 快速失败闸
（`_session_unhealthy`，仅拦自动路径）→ ops 卡「🔌 平台会话健康」+ 不健康 messenger 行
「重新登录」按钮（`POST /api/admin/platform-sessions/relogin` → Node `/accounts/:id/relogin`
同 profile 重启 + 30min 交互窗）。持续掉线由 `HealthWatchdog._check_platform_sessions`
升级式提醒（`health_watchdog.session_stale_remind`，默认 30min 首提/4h 重提，恢复自动清零）。
Node 侧不变量：composer 清空 + 回读气泡二次确认（失败标记→502 如实上报；回读不定态按已送达防重发刷屏）；
崩溃快自愈（退避×5）放弃后仍有 15min 慢重试兜底；WA 意外断线自动重连（修「假在线」）。
Messenger 自动化档位经 `inbox.auto_draft.platform_modes: {messenger: review}` 封顶
（`cap_automation_mode`，AI 只拟稿人审后发；恢复全自动删该行重启）。
messenger-web `start.ps1` 显式 `MSG_RESTORE_ON_BOOT=1`（headed 也开机恢复，解主进程启动顺序依赖）。

**质量评测门禁**（对外可信硬指标，缺资源优雅跳过，纯核心在 `src/eval/`）：
```bash
python -m pytest tests/test_faq_resolution_gate.py tests/test_translation_quality_gate.py \
 tests/test_memory_recall_eval.py tests/test_memory_extract_eval.py \
 tests/test_persona_consistency_eval.py tests/test_emotion_eval.py \
 tests/test_crisis_response_eval.py tests/test_translation_confidence.py \
 tests/test_proactive_guard_eval.py tests/test_crisis_resource_eval.py \
 tests/test_crisis_safety_overview.py tests/test_voice_language_eval.py -q --tb=line
```
- FAQ 自解决率：KB 备货(≥`AITR_FAQ_MIN_ENTRIES`)时强制 ≥`AITR_FAQ_RESOLVE_TARGET`；缺库/夹生库 skip。
- 翻译回译质量：src→tgt→src 回译相似度近似质量；可评引擎＝**确定性引擎(DeepL/Google)** 或
 **本地 MT(ollama_mt，评测器强制 temp=0 贪心=可复现)**，均缺 → skip。CLI `--xlate-engine
 auto|deterministic|ollama_mt|ai`（auto=DeepL/Google→ollama_mt 顺位；ai=DeepSeek 仅横比不进门禁）；
 evaluator 读 config 时会合并 `config.local.yaml` overlay 并对 Ollama 端点做 /api/show 探针（端点宕/
 模型缺→skip 而非全 0 假 FAIL）。本地 MT 实景门禁 opt-in：`AITR_XLATE_LOCAL_MT=1`（CI 默认不依赖
 局域网 GPU）。宽语种集 `config/eval/translation_samples_hymt.yaml`（30 样本×17 语）。
 阈值 `AITR_XLATE_SAMPLE_THRESHOLD`/`AITR_XLATE_PASS_TARGET`。
 CLI：`python -m scripts.run_eval --translation [--json]`。
 **语义轨**（P2）：有嵌入 provider（`embedding_providers.build_embed_fn`，本仓生产=140 bge-m3）时
 自动补嵌入余弦 `semantic`；字符轨不合格但语义 ≥ 阈（默认 0.8，`AITR_XLATE_SEM_THRESHOLD` /
 `--xlate-sem-threshold`）→ 按合格记 `rescued=True`——救「正确的意译」（「九折」→回译「10%的折扣」
 字符 0.39/语义 0.84）。阈值 0.8 依 bge-m3 实测校准：意译区 0.84-0.93 / 同域错义区 0.61-0.74 /
 跑题区 <0.42，落干净间隔中。嵌入失败软降级纯字符轨，绝不因端点抖动崩评测。`--xlate-semantic off` 关。
 **交叉回译**（P2）：`--xlate-back-engine same|deterministic|ollama_mt|ai`——同引擎自回译会给
 「复读自己措辞」的引擎虚高字符分；正/回向分属两引擎时偏置对称抵消，横比才公平。
 **周批趋势**：计划任务 `TranslationEvalWeekly`（周六 06:30，`scripts/translation_eval_weekly.ps1`）
 跑默认+宽集，`--out-jsonl` 摘要追加 `logs/eval/translation_trend.jsonl`。
 2026-07-11 实测基线（宽30样本×17语）：字符轨自回译 HY-MT 0.677 vs DeepSeek 0.750 看似落后，
 但**交叉回译+语义轨**下 HY-MT-fwd 0.933 vs DeepSeek-fwd 0.922——字符差距主要是复读偏置+意译压分
 假象；语义口径本地 MT 持平略胜（vi/es/ru +0.05~0.07，hi -0.075），两集 100% PASS（语义救回 3 例意译）。
- 记忆召回质量：真实 `EpisodicMemoryStore` 端到端跑 `get_bullets_for_prompt`，对比关键词 vs 向量融合召回率；
  机制自测用确定性本地嵌入(离线可复现)，真实语义增益需真实嵌入(不可用则 skip)。门禁 `top_k` 默认 3
  （须 < 每场景事实数才鉴别排序；实测 keyword 80% vs vector 100%/+20%）。
  CLI：`python -m scripts.run_eval --memory [--json]`。开向量召回走能力看板 `memory.vector.enabled` 治理化开启
  （非盲改默认；degrade-to-keyword 零阻断）。
- 记忆语义去重：跨真实 `merge_near_duplicates`(R5)，近义改写应并、异义事实不应过并；只在**真实语义嵌入**下有意义。
  CLI：`python -m scripts.run_eval --semantic-dedup [--dedup-threshold 0.7]`。门禁 `tests/test_memory_recall_eval.py`(缺嵌入 skip)。
- **真实嵌入 provider**（`src/eval/embedding_providers.py`，解锁上面两项从 skip→实跑）按序探测：
  ① OpenAI 兼容端点(env `AITR_EMBED_BASE_URL/MODEL/API_KEY` 或 config `ai.embedding_base_url/embedding_model`，
  LM Studio/Ollama/OpenAI)；② 本地 sentence-transformers(**opt-in** `AITR_EMBED_LOCAL=1`，默认多语
  `paraphrase-multilingual-MiniLM-L12-v2`，免 key、模型缓存后离线，避免默认 CI 背 torch 冷加载)；均无 → skip。
  生产开向量/去重：配 `ai.embedding_*` + `memory.vector.enabled` / `memory.consolidation.semantic_dedup`。
- 人设一致性（陪聊"真人感"最后防线）：`persona_guard` 是否抓全客服腔/AI 自曝（违规召回，漏一个=事故）
  且不误伤合规（含"我才不是AI啦"否定句）；纯函数常驻门禁。CLI：`python -m scripts.run_eval --persona`。
  阈值 `AITR_PERSONA_RECALL_TARGET`(默认 1.0)/`AITR_PERSONA_MAX_FP`(默认 0)。
- 情绪识别：① 情绪维度准确率(`analyze_emotion`，多分类，阈 `AITR_EMOTION_ACC_TARGET` 默认 0.8)；
  ② 危机识别(`detect_crisis` 安全红线，severe 召回须 1.0、惯用语零误报，`AITR_CRISIS_RECALL_TARGET`/`AITR_CRISIS_MAX_FALSE_ALARM`)。
  CLI：`python -m scripts.run_eval --emotion` / `--crisis`。
  **I 否定硬化**：`analyze_emotion` 情绪词命中加否定前瞻（不/没/别/not），「不难过/没那么累/别担心/not sad」
  不再误判负面/低能量；`tests/test_emotion_eval.py::test_negation_not_misclassified` 回归网量化。
- **危机响应闭环**（J，识别→处置端到端安全，安全侧最重门禁）：`src/eval/crisis_response_eval.py`
  复刻 `SkillManager._apply_crisis_safety_net`——severe/elevated 输入须注入安全指令(预防)；回复触自伤红线
  必被 `safe_fallback_reply` 整段覆盖、劝阻句(「别去死」)不可误覆盖；**终态输出 100% 不含鼓励自伤片段**(硬红线)。
  纯函数常驻门禁 `tests/test_crisis_response_eval.py`。CLI：`python -m scripts.run_eval --crisis-response`。
- **译文在线置信度 + 引擎智能切换**（K）：`src/ai/translation_confidence.py` 确定性评分(空/未翻译/错语种/长度异常)，
  `EngineRouter(min_confidence>0)` 在主引擎低置信时自动切换下一引擎择优(都不达标→最高分候选，不阻断)；
  生产开关 `translation.engines.confidence_switch.{enabled,min_confidence}`(默认关=旧行为)。scorer 门禁
  `tests/test_translation_confidence.py`(纯函数常驻)。CLI：`python -m scripts.run_eval --xlate-confidence`。
- **按语种引擎覆写 + 在线语义闸门**（K2）：`translation.engines.per_lang_order`（如 `{hi: [ai, ollama_mt]}`）
  把评测实锤的弱语对重排到强引擎优先——只重排 order 内引擎（未知名忽略），覆写外引擎按默认序补尾兜底，
  其余语种不受影响（hi 三样本 A/B：同 AI 回译口径 MT char=0.757 vs AI 0.884 → 已上线覆写）。
  `confidence_switch.semantic.{enabled,min_similarity}`（默认关）＝确定性信号的盲区补丁：确定性达标的译文
  再比对 源/译 跨语言 bge-m3 余弦（走 ai_client.embed，~50ms），低于阈值同低置信处理（切换/择优），
  嵌入失败/返空一律放行（fail-open 不阻塞）；源文 <4 有效字符（"OK"/"哈哈"类）直接跳过（嵌入噪声大且
  漂移风险≈0，省一次往返）。阈值 0.65 依宽语料 44 对离线校准：真实译文 min=0.712/p5=0.775，
  错配内容 max=0.741/p95=0.683（zh→fr/hi 正确译文天然低分 → 阈值再高会误切）。观测：
  `translation_engine_semantic_low_total`(Prom) / `metrics.translation_engines.semantic_low` / ops 卡「语义闸门拦截」
  （i18n 键 `ov2_js_sem_low`）。门禁 `tests/test_translation_engines.py`（覆写路由/兜底/describe + 语义切换/fail-open/短文本跳过/全低择优）。
  **嵌入双活**：`ai.embedding_base_urls`（列表，优先于单数键）= 140+176 双 bge-m3 端点，`ai_client.embed()`
  按序尝试、异常端点 60s 冷却降权（不剔除）、**全端点失败才计全局熔断 streak**（单点抖动零感知）；
  其他嵌入消费方（KB embed-all/eval provider/readiness）仍读单数键 `embedding_base_url`（保持 140）。
  门禁 `tests/test_ai_client_embed_failover.py` + readiness 认列表键（`tests/test_companion_embedding_readiness.py`）。
- **评测语料双向化**：`TransSample.source_lang` 显式标注源语（优先于探测——短句探测不可靠），反向进站样本
  （en/ja/ko/th/vi/id/es/ru→zh，12 条）已入宽集 `config/eval/translation_samples_hymt.yaml`（现 44 样本：
  zh→xx 32 + xx→zh 12；HY-MT 全绿 pass=44/44，语义均分 0.939，xx→zh 方向 char 均分 0.79 高于 zh→xx 0.68）。
- **主动护栏闭环**（L，情绪安全闸门）：`src/eval/proactive_guard_eval.py` 把所有主动路径共用的
  `proactive_emotion_gate` 当安全不变量回归——**severe 窗口内必 block**(漏判=最脆弱时还推剧情)、窗口外正确退化、
  负面末条→soft、正面/中性不过度沉默。门禁 `tests/test_proactive_guard_eval.py`。CLI：`--proactive-guard`。
- **翻译置信度上线观测**（M）：`TranslationEngineStats` 增 `low_confidence`/`confidence_switches` 计数，
  经 `dump()`→`/api/workspace/metrics`、`dump_prom()`→Prometheus(`translation_engine_low_confidence_total`/
  `..._confidence_switches_total`)，无需新路由；观测「切了多少、值不值」。
- **情绪强度分级**（N）：`analyze_emotion` 程度副词缩放 intensity(「有点累」0.39<「累」0.6<「累死了」0.78)，
  只改强度不改标签(→arousal/valence/记忆 salience，否定/维度判定不受影响)。门禁
  `tests/test_emotion_eval.py::test_intensity_grading_monotonic`。CLI：`--emotion-intensity`。
- **情绪强度落库 + 护栏分级**（O，打通 N→L）：ingest 用 `analyze_emotion` 量级补 `conversation_meta.last_emotion_intensity`
  (列默认 -1=未知；标签仍来自规则分类器，强度正交)；`proactive_emotion_gate(last_emotion_intensity,min_negative_intensity=0.5)`
  使「有点焦虑」(低强度)不抑制剧情邀约、「很焦虑」才 soft——**危机分级不受强度影响**、强度未知保守按旧行为。
  经 `build_proactive_opener`→`companion_proactive` 主动开场路径透传。门禁 `tests/test_proactive_guard_eval.py`。
- **翻译置信度看板**（P）：ops_overview 新增「🌐 翻译引擎」卡，读 `/api/workspace/metrics.translation_engines`
  展示翻译尝试/低置信率/智能切换次数/降级次数 + 每引擎成功率延迟（M 的计数可视化）。
- **危机资源保障**（Q，安全处置延伸）：`src/eval/crisis_resource_eval.py` 复刻 `_apply_crisis_safety_net` 资源分支——
  severe+开 `crisis_resource_assurance`+有热线+回复无资源→**补一次**(热线只现一次)、已含资源/非severe/无热线/关→不补、
  红线优先(有害先覆盖)。门禁 `tests/test_crisis_resource_eval.py`。CLI：`--crisis-resource`。
- **情绪强度全路径透传**（R，O 的覆盖补齐）：`last_emotion_intensity` 经 `daily_ritual`/`milestone_ritual` 透传进
  早晚安(`build_ritual_opener`)、纪念日/节日(`build_milestone_opener`)、槽位采集(`build_profile_ask_opener`)三条
  ritual 路径——与主动开场(O)同口径走 `proactive_emotion_gate` 强度分级（轻度负面不过度沉默）。
- **翻译置信度趋势化**（S，P 的时序延伸）：`src/ai/translation_trend_store.py`（仿 `tts_cost_store`，默认关）按日
  upsert {尝试/低置信/切换/语义闸门 sem_low}（旧库经幂等 ALTER 迁移补列），`/api/admin/translation-confidence-trend`
  读近 N 天，ops 看板出低置信率/切换率/语义闸门率 7 天 sparkline（语义线仅在有命中时显示）。
  开关 `translation.engines.confidence_switch.trend_log`；门禁 `tests/test_translation_trend_store.py`。
  **周批语对拆分**：`evaluate_translation_quality` 的 `summary.by_pair`（`{src->tgt: {n,passed,char_mean,sem_mean}}`）
  随 `--out-jsonl` 趋势行携带；`scripts/translation_eval_weekly.ps1` 宽语料一周三口径（默认集 + 宽集同引擎 +
  宽集交叉回译 `--xlate-back-engine ai`，行内 `back_engine` 区分）→ 弱语对该不该进 `per_lang_order` 直接读周数据。
- **危机安全总览**（T，整条安全链单一入口）：`src/eval/crisis_safety_overview.py` 聚合 L/O(主动抑制)+J(响应闭环)
  +Q(资源保障)为一张总览 + 合并 `passed`（全绿才绿），不引入新逻辑。门禁 `tests/test_crisis_safety_overview.py`，
  CLI：`python -m scripts.run_eval --crisis-overview [--json]`。
- 记忆**抽取**质量（源头质量，比召回更上游）：对消息跑真实抽取器，按 `expect`/`forbid` 子串算
  召回 + 误抽数。启发式抽取器(`extract_heuristic_facts`)是纯函数 → **常驻门禁**(召回≥`AITR_EXTRACT_RECALL_TARGET`
  且误抽≤`AITR_EXTRACT_MAX_FP`)；LLM 抽取(`ai_client.extract_memory_bullets`)缺 key → skip。
  CLI：`python -m scripts.run_eval --memory-extract [--extract-llm] [--json]`。
  （启发式自称/称呼正则已加动词/虚词护栏，防「我是说真的」类句子片段被误归名字污染长期记忆。）
- **语音合成语言一致性**（U，防「中文声纹念英文」）：`src/eval/voice_language_eval.py` 复刻发声路径共用的
  `voice_clone_client.effective_clone_language`——克隆合成送主机的 `language` 须随**待合成文本实际语种**
  （中文回复仍 zh=行为不变；英文/他语回复由默认 zh 纠正，防按中文音系发音 garble；无法判定/空→回落账号默认）。
  覆盖 autosend / 原生 voice_reply / 手动坐席三条链路同一瓶颈。纯函数常驻门禁 `tests/test_voice_language_eval.py`。
  CLI：`python -m scripts.run_eval --voice-language [--json]`。阈值 `AITR_VOICE_LANG_ACC_TARGET`(默认 1.0)。
- **语音情绪 GPU 化**（SER 远程主路）：176 音频服务（`scripts/asr176/`，与 GPU ASR 同进程同任务）加
  `POST /v1/audio/emotion`（emotion2vec_plus_large CUDA，warm ~44ms vs 117 CPU plus_base 秒级）；
  服务端只回 `{labels,scores}` 原始数组，**标签→系统语义映射仍在客户端** `speech_emotion.py` 单一出口。
  客户端 `speech_emotion.remote.{base_url,timeout_sec,cb_cooldown_sec}`（config.local）＝远程优先，
  失败进 120s 冷却回落本地 funasr CPU（远程可用时不受本地加载熔断牵连），语音链零阻断。
  观测：`SpeechEmotionStats.remote` → `speech_emotion_remote_total`(Prom) + ops「🎧 音频情绪」卡
  「远程 GPU 占比」（键 `ov2_js_se_remote`）。门禁 `tests/test_speech_emotion.py`（远程成功/失败回落/冷却/
  本地断路器不牵连/无 remote 旧行为）。模型获取教训见 `scripts/asr176/README.md`（176 hub 下载不可靠，
  117 下载→scp）。
- **176 音频服务自愈 + 预热**：`AITR_WARMUP`(默认 1) 启动即后台预载 ASR+SER（消重启后 ~15s/~6s 冷启，
  `/health` 出 `asr_loaded/ser_loaded`）；计划任务 `AITR_ASR_WATCHDOG`(每 5min) 跑 `watchdog_asr.ps1`
  ——health 8s 无响应经计划任务自动重启（ONSTART 只保开机，白天崩了会静默降级 CPU，看门狗闭环）。
- **视觉(VLM)双活**（`vision.base_urls`，2026-07）：176(5090,主)+140(4070,备)各备 `qwen2.5vl:7b`，
  `VisionClient` 多端点按序试、异常端点 60s 冷却降权（**模块级**状态——实例按调用即建即弃）；
  端点通但空答不切端点（省第二块 GPU），全端点异常仍走旧智谱云兜底。所有消费方
  （TG/LINE/Messenger/WA RPA + 图片翻译 OCR）经同一类自动获益。`_wants_openai_primary`/
  `has_any_vision_backend` 认 `base_urls`。门禁 `tests/test_vision_fallback.py`（解析/切换/冷却重排/
  全冷却硬试/空答不切）。140 冷载实测 130s（timeout 150 覆盖）、热态 ~5s。
- **166 旧主机引用清理**（网段迁移遗留，2026-07-11）：`messenger_rpa.audio_pipeline` → 176 GPU ASR
  （同 OpenAI 契约）；`whatsapp_rpa.voice_output` coqui_http→166 改 `minicpm_clone` 本机 IndexTTS2
  （与 TG voice_reply 同栈，失败回落 edge_tts）；`ai.embedding_base_url` 基线值 192.168.1.43(旧 Wi-Fi)
  → 192.168.0.140。`faceswap`(166:8000) 无替代主机，已知死配置待产品决策。140 双默认网关经核实
  metric 已分明（以太网 25 vs WLAN 326，Windows 自动降权），不动网络配置。
- **翻译趋势周报 CLI**：`python -m scripts.xlate_trend_report [--json]` 把周批 JSONL 按
  (dataset,engine,back_engine) 分组渲染趋势表 + 最新弱语对 Top-K（sem 升序，n<2 标注），
  周审读数即可决策 per_lang_order/阈值。门禁 `tests/test_xlate_trend_report.py`。
  首次周审（2026-07-12，44 样本）：交叉回译(back=ai) sem 0.945 vs 自回译 0.939——**自洽虚高未坐实**
  （交叉口径反而略高，指标可信）；弱语对 zh→ar 0.899 / zh→fr 0.911 / zh→hi 0.923 全过线，
  **不动 per_lang_order**。反向语料按弱语对补 6 条（ru/id/ar/fr/hi→zh，corpus 44→50），
  下周趋势行 n≥2 可稳读。周批任务 TranslationEvalWeekly 建于上周六后，首次自动跑在 7/18。
- **主对话 LLM 容灾**（`ai.fallback`，2026-07-12）：DeepSeek 云不可达（两次尝试失败）或熔断开路时，
  `AIClient` 回落 176 本地 `qwen3:30b-a3b-instruct`（MoE 3B 激活，热答 ~2-3s）**出真话**，替代
  canned 占位句；复用主链已构建 messages（人设/记忆/上下文全保留）+ 末位语言钉子（本地小模型
  易混语），语言守卫照常；兜底自身失败仍回 canned（最差不劣于旧链）。Ollama 端点自动走**原生
  /api/chat**（/v1 兼容层不认 keep_alive/think——实测被忽略），`keep_alive:30m` 断云期驻留显存
  （5090 上 18G 与 MT/bge 共存），只有首个用户吃 ~15-27s 冷载。主客户端同轮改为连接 5s 快败 +
  关 SDK 内建重试（调用方自有 2 次循环），断云→出话从分钟级降至 ~8-20s；熔断开路期 0 主链开销。
  观测：`/api/bot-metrics.local_llm_fallback{calls,ok}` + dashboard 质量行「本地兜底出话」+
  llm_cost tier=local_fallback。门禁 `tests/test_ai_client_chat_fallback.py`（主成功不兜底/主挂兜底/
  开路直兜底/双挂回 canned/原生口选择/配置解析）。演练：死主端点 → 首答 20.3s（含冷载）、次答 8.4s。
- **176 音频服务健康灯**（2026-07-12）：`collect_health` 周期探自建 GPU 音频服务 `/health`
  （`audio_probe_target` 纯函数决策：仅 voice_recognition 启用 + OpenAI 兼容 + **私网** base_url 才探，
  公网云 ASR 无此契约不探防误报；60s TTL 缓存 + 3s 超时），出 `audio` 组件进运行时健康：不可达/
  模型未装载→**warn 黄灯**（链路自动降级 CPU 属软性），装载齐→ok。挂 → 看板黄灯 + problems 可见，
  补掉「176 服务挂了只有远端看门狗知道」的主站盲区。门禁 `tests/test_audio_service_health.py`。
- **断云真流量演习**（2026-07-12 凌晨低峰实施，结论可信）：防火墙封 DeepSeek 出站 → 经
  `/api/copilot/query`（真 `generate_reply` 全链）打 22 发含 9/10 并发：**22/22 由本地兜底出真话**
  （热答 4.3-6.8s，并发不塌），熔断走完 closed→open（跳过主链，答复 1.8s）→半开→**探测成功自动闭合**
  全周期，全程 0 canned。防火墙 RST 场景主链快败 ~2s；真黑洞(丢包)场景每次开路前调用付 ~11.5s
  → `ai.circuit_breaker.window_size` 经 overlay 降为 10（10 发内 ≥5 失败即熔断，减半慢调用敞口）。
- **AvatarHub 本机语音接入**（`avatar_voice`，2026-07-12）：本机(117) 即 AvatarHub TTS 节点
  （D:/faceX/mfys 常驻，计划任务自启）——`src/ai/avatar_voice.py` 薄 HTTP 客户端封装
  7852 CosyVoice3 情感克隆（在线主力 2~4s/句：/v1/tts/clone emotion 标签 + /v1/tts/instruct
  自由语气 + register_spk 启动预热）、7858 Qwen3-TTS（RTF≈2.8 仅离线批量，CLI
  `scripts/avatar_prerender.py`）、远端 140:7854 Whisper STT（X-AH-Svc 令牌**运行时**读
  D:/faceX/mfys/secrets/service_token.txt，绝不入库）。**铁律：只 HTTP 调用，本进程严禁加载
  TTS/GPU 模型（3060 显存已满，有过 OOM 事故）**；模块级 GPU 串行锁（7852/7858 同卡），
  单请求 90s + 重试 1 次；长回复复用 split_text_for_clone 切块（≤80 字）拼接。
  接线：TTSPipeline 新后端 `avatar_clone`（health 探测→合成→失败回落 edge，与 minicpm_clone
  同模式）；EmotionSpec→7852 词表映射 `voice_emotion.to_cosyvoice_emotion`（warm/empathetic→
  gentle，neutral→每角色 emotion_default）；STT 进 `voice_recognition.fallback` 级联第 2 级
  （176 GPU → **140 AvatarHub** → 本机 CPU）；参考音逐字稿 sidecar 自动发现（ref.wav 旁同名
  .txt，`find_reference_text`）；main.py 启动后台预热（服务没起先 schtasks 拉起：
  EmotionTTS_Boot/Qwen3TTS_Boot）。telegram.voice_reply 与 profiles_runtime 全部人设已切
  `avatar_clone`（旧 minicpm_clone/7899 常不在线，实际一直在回落 edge 通用声）。
  门禁 `tests/test_avatar_voice.py`（36 例：纯函数/串行锁/重试/管线接线/降级/STT 令牌）。
  **Phase2**（同日）：① **预渲染命中层** `src/ai/voice_prerender.py`——固定台词命中
  `assets/voices/<persona>/prerendered/<sha1(归一化文本)8>.ogg`（sidecar .txt 逐字校验防
  碰撞/陈旧）直接发，零 GPU 零延迟（实测 ~200ms vs 合成 ~7s）；渲染与查询同一键函数，
  CLI `--force` 重渲（换参考音后必须）；挂在 TTSPipeline.synthesize 内存缓存**之前**
  （缓存可能存 edge 兜底声，预渲染音质更优）；persona_id 经 resolve_voice_cfg 注入。
  ② **动态语气指令**（`avatar_voice.dynamic_instruct`，overlay 已开）：情绪非中性时
  `voice_emotion.to_cosyvoice_instruct` 模板库（10 情绪×2-3 变体，crc32(文本) 确定性轮换
  =缓存友好）生成自由语气指令走 /v1/tts/instruct，比 11 个标签细腻；`voice_profile.instruct`
  静态配置永远最高优先；真机 A/B：instruct 通道中英文合成 STT 回转全对。多语种护栏
  **实验后判定不需要**：7852 对英文文本自动跟语种（emotion 标签通道 STT 回转 overlap=1.00），
  无需 language 指令。③ **观测**：`avatar_voice_stats.py`（合成成败/延迟/通道/预渲染命中/
  GPU 队列水位/STT）→ `/api/voice/avatar-status`（三端点并行健康+备货量）+
  `metrics.avatar_voice` + Prometheus `avatar_voice_*` + ops-overview「🎙️ AvatarHub 语音」卡
  （i18n `ov2_s_avatar`/`ov2_av_*` zh+en）。④ **enroll 收编**：`/api/voice/enroll` 最优先走
  AvatarHub（7852 在线即零样本登记 + STT 自动产逐字稿 sidecar + register_spk 后台预热），
  纯函数 `voice_enroll.build_avatar_voice_profile`；whatsapp_rpa.voice_output 同步切
  `avatar_clone`。⑤ 7858 为**懒加载**服务（空闲卸载省显存，首请求 503 + 后台冷载 ~2min）
  → 预渲染 CLI 批级自愈（失败→ensure_ready 轮询→整批重试一次）。
  门禁 `tests/test_avatar_voice_phase2.py`（预渲染纯函数/命中零合成/sidecar 防错发/
  动态 instruct 确定性/通道切换/stats/enroll profile）。
  **Phase3**（同日，备货闭环+人设声线+健康灯）：① **台词库+夜间自动备货**：
  `config/prerender_lines/`（`_common.txt` 全人设共用 + `<persona>.txt` 专属，行级注释/
  归一化去重，reader=`voice_prerender.read_prerender_lines`）；CLI `--all-personas` 自动收集
  avatar_clone 人设（profiles_runtime ∪ config personas）批量渲染；计划任务
  **AvatarPrerenderNightly**（每日 04:30，`scripts/avatar_prerender_nightly.ps1`，日志
  `logs/prerender/` 保留 14 份，顺带 register_spk 预热）。首跑实战验证批级自愈：7858 崩溃
  → ensure_ready 经 Qwen3TTS_Boot 拉起 + 冷载轮询 → 整批重试成功（7 人设 73 条备齐）。
  ② **备货缺口观测**：短句（≤`prerender.miss_track_max_chars`=16 字）查预渲染未命中 →
  `record_prerender_miss` 记 Top-N 缺口台词（distinct 上限 50 防撑爆；AI 出站短句非用户隐私）
  + `prerender_coverage` 覆盖率 → status API / ops 卡「备货缺口 Top」/ Prom
  `avatar_voice_prerender_miss_total`——运营照单往台词库加词即闭环。
  ③ **人设声线底色**（`voice_profile.instruct_style`）：词表 `_INSTRUCT_STYLES`
  （撒娇/俏皮/温柔/御姐/沉稳/清冷/阳光）与情绪内核复合成「用<底色>、<内核>的语气说」——
  同是 warm 撒娇与沉稳念出来是两个人；底色与内核语义重叠（俏皮×playful）自动去重防
  「活泼俏皮、俏皮活泼」冗余；7 个运行时人设已配。④ **健康灯**：`avatar_probe_target`/
  `probe_avatar_voice`（60s TTL + 3s 超时，仅探 7852——7858 懒加载/STT 多级兜底属正常态
  不进灯防误报）→ build_health `avatar_voice` 组件（不可达/未载入=warn 黄灯软降级，
  绝不红灯）。  门禁扩到 `tests/test_avatar_voice_phase2.py`（缺口计数/cap/覆盖率/长短句
  分界/底色复合/重叠去重/台词库合并去重/探测决策/缓存/健康组件三态）。
  **Phase4**（同日，缺口一键闭环+主动告警+GPU 省耗）：① **一键入库**：ops 卡缺口行
  「入库」按钮 → `POST /api/voice/prerender-lines/add`（`GET .../prerender-lines` 列清单）
  → `voice_prerender.append_prerender_line`（目标净化防路径穿越 / 归一化键去重 /
  60 字长度守卫 / 保注释追加）写 `_common.txt` + `render_now` 后台拉起
  `--all-personas` 增量渲染子进程（`_spawn_prerender_render` 防重复；复用 CLI 全套
  7858 自愈）。实测：入库→~2min 全 7 人设备货→线上即 `provider=prerendered` 命中。
  ② **同音色跨人设复用**：`render_persona(ref_cache)` 按参考音指纹（路径+size+mtime）
  缓存本轮成品——同音色同台词只烧一次 GPU、其余人设直接复制（7 人设仅 2 音色 →
  夜间 GPU 省 ~70%；字节级验证同组一致/跨组不同）。③ **升级式主动告警**：
  `HealthWatchdog._check_avatar_voice`（配置 `health_watchdog.avatar_voice_remind.
  {enabled,after_min=30,interval_min=240}` 默认开）——7852 掉线 ≥30min 首提
  EventBus `avatar_voice_alert`（webhook 订阅别名 `avatar_voice`，`rate_key`
  独立限流），4h 重提，恢复补发恢复通知；未告警过的抖动恢复不发（防噪）；
  avatar_voice 未启用 probe=None 天然静默。  ④ 夜间任务时序：CLI 就绪等待 300→600s
  （7858 冷载实测可 3-4min，首跑曾踩 300s 超时）。门禁续扩 `test_avatar_voice_phase2.py`
  （入库目标净化/去重/保注释/看门狗时序全路径/静默路径/webhook 别名+文案/同音色复用
  合成次数）。
  **Phase5**（同日，备货生命周期+缺口全自动+人设归属）：① **参考音指纹生命周期**
  （防「换声后发旧音色」事故，安全项）：渲染登记 `_ref.json`（**内容 sha1**，
  `ref_content_fp` 按 size+mtime_ns 进程缓存——热路零重复读盘）；命中层第 3 重校验
  `stock_is_stale`（人设当前 ref 指纹 ≠ 登记 → 拒命中回落现场合成=正确音色，零错声
  窗口；无登记 legacy 放行向后兼容）；渲染侧检测漂移 → **自动整目录重渲**（否则旧
  clips 被「文件已存在」skip 卡死）+ 登记新指纹——换声次日自动恢复零延迟命中，
  `--force` 从必须人工变成保险。线上实证：篡改指纹 → 同句 `provider=avatar_clone`
  + 看板 `stale:1(zhao_laoshi)`；恢复 → 立即 `prerendered`。② **缺口自动入库**
  （`avatar_voice.prerender.auto_stock.{enabled,min_count,max_per_day}` 基线关/本机开）：
  watchdog 每小时扫缺口 Top-N → `qualify_auto_stock` 守卫（频次阈值/≤16字/无数字/
  无URL/敏感词表 转账·验证码·微信号等——宁可漏进不错进）→ 单人设占比 ≥80% 进专属库
  否则 `_common` → 每日预算（默认 10）防灌爆；渲染交夜间任务。「看缺口→补台词→
  渲染」全程零人工。③ **缺口人设归属**：`record_prerender_miss(text, persona_id)`
  （每文本 ≤8 人设 capped），top_misses 带 `personas`，ops 卡单一归属显示 `(pid)`，
  auto-stock 据此选目标库。④ webhook 演练结论：`notify_webhooks.json` 已有
  boss-telegram 渠道（`enabled:false`，**开启是运营决策**——开了所有订阅事件都真推），
  已把 `avatar_voice` 别名加入其订阅；formatter/别名/升级时序由 360 例门禁覆盖。
  门禁续扩（指纹缓存失效/生命周期全路径/管线拒陈旧/渲染自动 force/auto-stock 守卫
  ×7/目标路由/预算节流/归属统计）。
  **Phase6**（2026-07-13 凌晨，全自动闭环验收+STT 语言语义修正）：① **零人工闭环
  端到端实证**：17:45 三次同短句缺口 → 18:36 watchdog 自动入库（lin_jiaxin 专属库，
  100% 归属）→ 03:27 夜间任务渲染成声（47f0ea0f.ogg）→ 线上同句 `provider=prerendered`
  零延迟命中——「缺口→入库→渲染→命中」全程无人。② **7854 契约探明**（OpenAPI）：
  `/transcribe_b64 {audio_base64, language(默认zh), task}`、`/translate {text,src,dest}`
  （NLLB-600M，zh→en 实测 ~70ms）、`/translate/langs` 多语种、`/asr/load|unload`。
  ③ **language 语义是坑**（实测）：具体语种=Whisper**强制转写语言**——英文音频+zh
  可能输出中文**译文**而非转写（行为还不稳定）；`"auto"` 直接 500；**空串=服务端自动
  检测**（正确档）。修正：`build_stt_payload` 归一化 auto→空串；AvatarWhisperTranscriber
  不再把 auto 硬编成 zh（修复前外语语音走 140 回落层会被翻成中文→AI 误判用户语言）。
  端到端验证：英文语音 → auto → 逐字正确英文转写。④ NLLB 翻译**刻意不接入**
  `translation.engines` 栈（质量<在栈 hy-mt2-7b/DeepSeek 且栈已双活）；仅保留
  `AvatarVoiceClient.translate()` 工具方法。跨语言语音闭环判定＝**已天然工作**
  （STT auto → LLM 按客户语言回复（既有守卫）→ 7852 原生多语合成），零新基建。
  ⑤ 音色上传 UI 判定＝已存在（出货副驾组件 cp-voice.js 走 `/api/voice/enroll`，
  Phase2 的 AvatarHub-first 登记流自动生效）。⑥ 夜间日志 UTF-8 修正
  （PYTHONIOENCODING + Out-File，PS5 `*>>` 混写 UTF-16 乱码）。
  **Phase7**（2026-07-13，「活人感」冲刺——运营方针：拟人化/情绪价值 > 时延）：
  ① **副语言标记注入** `voice_emotion.inject_paralinguistic`——按情绪把 CosyVoice3
  原生标记注入合成文本：sad/empathetic → 句首 `[sigh]`/叹词后+逗号气口 `[breath]`；
  playful/happy/excited → **只在文本自带笑点**（哈哈/太逗/笑死…）后插 `[laughter]`
  （没笑点硬笑=恐怖谷，刻意不做）；serious/apologetic/neutral 不注入。确定性
  （crc32(文本) 定注入与位置=TTS 缓存安全）、每条 ≤max_marks(2)、intensity 缩放
  概率（<0.25 不注入）、已手工标注不叠加。真机 A/B（样本 tmp_tts_preview/paraling/）：
  [breath]/[laughter]/[sigh]/<strong> 四标记 STT 回转全零 garble（tokenizer 层消费
  绝不读出）。开关 `avatar_voice.paralinguistic.{enabled,max_marks}`（基线关/本机开）
  + 人设级 `voice_profile.paralinguistic: false` 可关。② **情绪变速曲线**
  `cosyvoice_speed`：情绪自带默认速度（sad 0.90/empathetic 0.93/excited 1.08…），
  pace 相乘微调，限幅 [0.85,1.15]（过度变速=机器感）。③ **分条语音发送**
  （`telegram.voice_reply.split_send`，基线关/本机开）：长回复（≥24 字）像真人一样
  连发 2-3 条短语音（`pack_voice_parts` 句级打包 ≤40 字/条，余量并末条）；
  **先全部合成成功才逐条发**（节奏是编排的，不被 GPU 进度驱动——运营方针的直接
  体现），条间间隔 ≈ 下一条音频时长×gap_factor + 真随机思考抖动，间隔期挂
  Telegram「正在录音」chat action（`_voice_recording_gap` 每 4s 续挂）；仅首条
  reply 引用原消息；每条各记外发计数、镜像一次 `[语音]×N`；任一条合成失败→
  整体回落单条整段路径（绝不「说一半」），中途投递失败→已发算数剩余丢弃。
  ④ 队列等待分段观测 `avg_queue_wait_ms`（容量规划：等待 vs 合成各占多少）。
  门禁 `tests/test_avatar_voice_phase7.py`（14 例：注入语义×6/速度曲线/打包/管线
  接线+人设 opt-out/分条全路径×4/队列观测）。
  **Phase8**（2026-07-13，「精神错乱」幻觉事故三防线）：真实事故（tg 5433982810）——
  用户发中文「好呀好呀」，AI 答「突然换日文了！那我也用日文回你」并复读「你说想去
  大阪玩」（用户从没说过）。根因＝幻觉自我强化链：① LLM 记忆抽取把 **AI 自己回复里
  的臆测**存成「用户事实」（AI 问「明天不用上班？」→ 抽出「用户明天不用上班」入库
  → 下轮注入 prompt → AI 更确信）；② 10 天断层后历史窗口旧轮次被当「刚才」；③ 历史
  里旧日语轮次+AI 自己的语言点评诱发「换日文」幻觉复发。三防线（均纯函数+事故案例
  回归网）：**A. 记忆接地护栏** `src/ai/memory_grounding.py`——抽出事实必须与用户
  原话有内容级词汇重叠（CJK bigram/拉丁词/数字；「宁可漏记不可错记」），
  `AIClient._ground_extracted_facts` 接线 + extract prompt 硬化（「只在助手回复出现
  的猜测/问句内容不得输出」）；丢弃记 WARNING 可观测。**B. 时间断层提示**
  `inbound_enrich.build_time_gap_hint`——`_turn_gap_sec`（skill_manager 于 update 前
  取旧 last_message_time 计算）≥6h 注入「距上次聊天已 X 小时/天，旧轮次不是刚才，
  只提对方亲口说过的内容」。**C. 语言事实钉子** `build_language_anchor_hint`——本条
  中文 && 历史含日文假名/韩文或 AI 语言点评痕迹 → 注入「对方本条是中文没换语言，
  勿提换语言」（条件克制：纯中文历史不注入）。  三提示汇入既有 `_topic_switch_hint`
  消费口。已清理该用户 3 条幻觉记忆（大阪/不用上班/深夜在线）。
  门禁 `tests/test_memory_grounding.py`（9 例，全部用事故真实语料）。
  **Phase9**（2026-07-13，音色保真复盘——用户「没用克隆声，像豆包」）：根因＝7852
  `/v1/tts/clone` 的路径选择：**非 neutral emotion → inference_instruct2，完全忽略
  reference_text 逐字稿** → 音色漂移成标准 AI 女声；而我们「无信号默认 gentle」+
  情绪层几乎每条派生非中性 + dynamic_instruct，把 100% 语音推进了 instruct2，
  逐字稿从未被用上（neutral+逐字稿走 inference_zero_shot 才是音色最像的路径；
  真机 A/B 样本 tmp_tts_preview/fidelity_ab/）。修复＝**音色保真优先**：
  ① `to_cosyvoice_emotion` 语义重构——弱情绪（intensity < `STRONG_EMOTION_THRESHOLD`
  =0.7，日常闲聊 0.6 全命中）归 neutral 走保真路径，情绪表达由**副语言标记+变速**
  承担（两者在保真路径同样生效，真机验证）；强情绪（真难过/亲密 0.75+/CSAT 0.8/
  声学情绪 0.78）才切情感路径用音色换表现力。阈值 `avatar_voice.
  emotion_channel_threshold` 可配。② `default_emotion`/`DEFAULT_EMOTION`/人设
  `emotion_default` 全部 gentle→neutral。③ `dynamic_instruct` 关（overlay）——
  `/v1/tts/instruct` 同样 instruct2 无逐字稿。④ 变速下限收紧 0.85→0.90
  （真机实测 0.93 以下咬字模糊：「失落」被转写成「示弱」）。⑤ 附带修注入位 bug：
  `_SIGH_LEAD_RE` 长叹词在前，防「呜呜」被截成「呜[sigh]呜」。生产管线双路径
  验证：弱情绪 → emotion=neutral+has_ref_text=True；强情绪 sad(0.9) → sad 标签。
  ⚠ 服务端改进项（集群侧待办）：instruct2 路径若能带 zero_shot spk 缓存/逐字稿，
  情感与音色可兼得——当前客户端只能二选一。
  **Phase10**（2026-07-13，情感×音色兼得+声纹量化观测）：① **服务端混合保真模式**
  （改 D:/faceX/mfys/emotion_tts_server.py，备份 .bak_20260713_hybrid，
  env `EMOTION_TTS_HYBRID=0` 可回退）：读 CosyVoice 源码确证 instruct2 的音色代价
  ＝`frontend_instruct2` **删除 llm_prompt_speech_token**（LLM 失去参考音语音前文
  → 韵律/音色都不跟参考音）；混合模式＝emotion+reference_text 同给时改走
  `inference_zero_shot` **全条件**（保留语音 token=保音色保韵律），情感指令拼进
  prompt 前缀（`"You are a helpful assistant. {desc}<|endofprompt|>"+逐字稿`，
  与 CosyVoice3 instruct 训练格式同构）。客户端 payload 本就带 reference_text，
  零改动自动受益：**强情绪场景从「音色换表现力」变成「音色情感兼得」**。
  ② **campplus 声纹量化观测**：`scripts/voice_similarity_probe.py`（复用集群
  clone_scorer，CPU onnx 28MB 不违显存纪律）每人设固定探针句合成→与参考音比声纹
  相似度，追加 `logs/voice_similarity.jsonl`，nightly 任务顺带跑；同参考音去重
  （7 人设 2 音色只合成 2 次）。**刻度认知**（8 样本实测）：正常带 0.78~0.86，
  声纹分只能抓**灾难级**漂移（<0.70 warn/<0.60 critical=换错参考音/文件坏/模型
  退化），**分不出「播音腔 vs 自然韵律」**（zero_shot 0.79 vs instruct2 0.81 持平
  ——「像豆包」的听感来自韵律模板化而非声纹漂移，campplus 测不出，仍需人耳）。
  ③ 客户端 threshold=0.7 **维持不变**（保守）：弱情绪纯保真=韵律最自然；强情绪
  走混合模式=情感前缀+音色保真。若试听后觉得情感浓度不足可下调阈值放量混合模式。
  门禁：`test_similarity_probe_classify`（分级纯函数）。
  **Phase11**（2026-07-13，韵律之眼+拟声词治理）：① **韵律自然度进探针**：集群
  `prosody_scorer`（纯 numpy：F0 半音 std/能量 dB std/浊音比 vs 参考音基准）接入
  `voice_similarity_probe` 第二指标——12 样本三通道分辨力实测：zero_shot 0.955 /
  混合 0.961 / instruct2 0.948（能量出 instruct2 的韵律模板化，方向与人耳一致；
  **且证实混合模式在声纹+韵律双指标上都无代价**，情感前缀是纯增益）。刻度未稳
  （n=12）→ jsonl 记 `naturalness` 字段**只收集不告警**，几晚数据后校准阈值再接
  告警。prosody_scorer 不可用时优雅降级仅测声纹。② **哭声拟声词治理**（注入器
  第 0 步，不占概率位）：sad/empathetic 时句首「呜呜/嘤嘤/哇哇」（≥2 连字）→
  `[sigh]`——TTS 念拟声词发音不稳（真机 STT 把「呜呜」听成「喂鱼」），文字拟声
  本就是副语言，真叹气声既稳又更像活人哽咽；防叠加（规整产物句首已是标记 →
  句首叹气步骤跳过）；playful 等非哭情绪不动拟声词（「呜呜」也可能是撒娇调侃）。
  门禁续扩 phase7 文件（规整/多连字/嘤嘤/不叠加/非 sad 不动 ×5 例）。
  **Phase12**（2026-07-13，情感放量+自然度自动守门+体检可视化）：① **放量**：
  `emotion_channel_threshold` 0.7→0.5（overlay）——混合保真模式经双指标验证无代价
  后，日常情绪(0.6)也带情感基调。**配套守卫**（`_try_avatar_clone`）：无逐字稿的
  人设发情感标签会掉服务端 instruct2（音色漂移）→ 客户端强制 neutral 保音色；
  即「情感标签必须配逐字稿」不变量，放量后无逐字稿人设零风险。② **自然度告警
  自动到期启用**：`calibrate_naturalness_floor`（历史 jsonl p10-0.05，样本 <15 返 0
  =只收集）——夜间每晚攒 2 音色样本，约一周后**自动**开始守门（低于下限 → 探针
  最差级升 warn，不改 critical/exit 语义——播音腔回归非灾难级）；无需人工回来
  定阈值。③ **音色体检进看板**：avatar-status 读 jsonl 尾部出 `voice_quality`
  （每人设最新 声纹/自然度/日期），ops 卡「音色体检」行（同音色组去重展示，
 i18n `ov2_av_vq*`）。门禁续扩（floor 校准/样本不足/脏行/jsonl 读取/无逐字稿
 守卫 ×5 例）。
 **Phase13**（2026-07-14，「半死」防线——13:42–16:01 事故：7852 /health 一直 200、
 register_spk 正常，但 /v1/tts/clone 全部超时 → 全部语音静默回落 edge 2h20m，
 health-only 探测全程绿灯零告警，服务端 `start /MIN` 无文件日志第一现场全失）：
 ① **合成级外部看门狗** `scripts/watchdog_emotion_tts.ps1`（计划任务
 **EmotionTTSWatchdog** 每 5min；ASCII-only 防 PS5.1 GBK 解码坑）——/health 三态
 （down→触发 EmotionTTS_Boot；loading 超 15min 宽限→按卡死重启；ok→**真打
 /v1/tts/clone 短句探针**（生产参考音+sidecar 逐字稿，120s 超时），两振确认才
 杀进程+经 Boot 任务拉回（30min 重启冷却防抖振循环），state 落
 `logs/watchdog_emotion_tts.state.json`、日志自轮转；`-DryRun` 只探不动，
 `-ForceRestart` 实弹演练（已真机验证：kill→Boot 拉回→冷载 103s→恢复→探针绿）。
 ② **服务端文件日志**：`D:\faceX\_svc_emotion_boot.bat` 重写对齐 qwen3 模式
 （备份 .bak_20260714_watchdog）——幂等判据从「端口 LISTENING」升级为 **/health
 200**（活着但 accept 死的僵尸会被收割），stdout/stderr 落
 `D:\faceX\logs\emotion_tts.out.log`（>20MB 轮转一份），下次事故有第一现场。
 ③ **主站半死告警**：`avatar_voice_stats` 增 `hang_signal()`（合成连败 streak +
 最近成败时间戳；成功清零），`_check_avatar_voice` 在 probe 绿时叠加判
 「连败 ≥`hang_fail_streak`(3) 且最近失败在 `hang_fresh_min`(20) 内」→ kind=hang
 走同一套升级提醒（`hang_after_min` 默认 20min=给外部看门狗自愈窗，它没救回来
 才轰人）；**hang 的恢复要正面证据**（失败后真的又成过一次）——无流量证据陈旧
 既不误报恢复也不重提；webhook 文案区分「合成挂死」并指路 emotion_tts.out.log。
 配置 `health_watchdog.avatar_voice_remind.{hang_fail_streak,hang_fresh_min,
 hang_after_min}`（随父开关默认开，streak=0 可关）。门禁续扩 phase2 文件
 （hang 激活/首提/陈旧不误报/正面证据恢复/成功清 streak/字段契约/webhook 半死
 文案 ×4 例）。附带修：`normalize_prerender_text` 断言随 clean_text_for_tts
 「换行折空格」新语义更新；`test_voice_routing` 假 TTS 补 `pre_colloquialized`
 形参对齐真签名。
- **LAN GPU 显存水位卡**（`ops.gpu_watermark`，2026-07-12）：ops-overview 新卡聚合各 Ollama 主机
 `/api/ps`（`src/utils/gpu_watermark.py` 纯函数 summarize + 30s TTL 探针，路由
 `/api/admin/gpu-watermark`），口径=Ollama 管理的模型显存（非 nvidia-smi 全卡）；≥75% warn/≥90% high，
 探不到=unknown（整队取最差不装绿）。「140(12G) 兼任嵌入+视觉备点被同时压上会挤爆」从 SSH 肉眼
 `ollama ps` 变成看板常驻可见。新子系统默认 enabled:false，本机 overlay 已开（176/140 两主机）。
 门禁 `tests/test_gpu_watermark.py`；卡片未启用时整卡隐藏。
- **云端故障告警闭环**（`host_alert` v2，2026-07-12 下午）：告警出口 `src/utils/host_alert.py`＝
 日志（logger 挂 `ai_chat_assistant.host_alert` → 落 app.log）+ EventBus `host_alert` 事件镜像
 （notifier 订阅别名 `host_alert`，配 webhook 即可外发 Telegram）+ Windows 弹窗（**只弹算力机**：
 用户端桌面 `AITR_DESKTOP_MODE=1` 与测试 `HOST_ALERT_SILENT=1` 只记录不弹；pytest 全局静默在
 tests/conftest.py）。触发面：① key 失效（401/402/403/quota 特征）——启动连接测试 + **openai-compat
 运行时双失败**（修「余额运行中扣完却静默到重启」盲区）+ gemini 路径；占位/未配 key（桌面首启）不弹；
 ② 熔断开路 → `notify_cloud_outage`（覆盖无 key 特征的网络黑洞，文案带本地兜底是否顶班），半开恢复
 补「云端 AI 已恢复」；③ DeepSeek 余额水位：`src/utils/cloud_credentials.py`（纯函数 target/classify +
 TTL 探针打 `/user/balance`）经 `HealthWatchdog._check_cloud_balance`（1h 节流；`ops.cloud_credentials`
 默认关、本机 overlay 开、<¥20 弹、6h 重提、余额接口 401 转 key 告警）；④ 本地兜底长期顶班升级提醒
 `_check_local_fallback_duty`（`health_watchdog.fallback_duty_remind` 默认开：顶班 30min 首提/4h 重提，
 连续 2 个无增量 tick 重置）。告警标签统一 `model @ host`（如 `deepseek-chat @ api.deepseek.com`）。
 智谱 vision key 已 401 失效 → overlay `vision.zhipu_api_key: ''` 停用云端兜底（LAN 176/140 双活为主路，
 待多云 key 备用池再接新 key）。门禁 `tests/test_host_alert.py` / `test_cloud_credentials.py` /
 `test_cloud_outage_alert.py`；新发布点已登记 `test_alert_delivery_e2e._EMITTED_ALERTS`。
- **云端 Key 备用池**（`ai.key_pool`，2026-07-12 下午）：主 Key 失效（欠费/被封/云故障）时聊天
 降级链＝主(2次)→**池内备用云 Key 逐个一次**（120s 失败冷却；条目缺省继承主链 base_url/model，
 同厂商备用号只填 api_key，跨厂商任意 OpenAI 兼容端点均可）→本地 fallback→canned；熔断开路期
 同序（池先于本地，主模型免打扰）。与主 Key 相同/池内重复条目自动去重；占位 key 跳过。池接管
 时发「备用 Key 已顶班」提醒（6h 去抖），池 key 自身 401/quota 也告警（防备用键悄悄过期）；
 DeepSeek 系池 key 余额随 `ops.cloud_credentials` 一并巡检（`balance_targets` 主+池去重，
 `collect_cloud_balances` 列表口径，逐 key 独立告警）。观测：`get_stats().key_pool_*` +
 llm_cost tier=`key_pool`。本机 overlay 已备 `key_pool.keys: []` 空池脚手架（填 key 即生效）；
 告警外发渠道脚手架 `config/notify_webhooks.json`（enabled:false，填 bot token+chat_id 后
 boss Telegram 可收全部主机告警；文件已进 .gitignore）。门禁 `tests/test_ai_key_pool.py`。
 **池 key 主动探活 + 管理 UI**（同日晚续）：`ops.cloud_credentials.chat_ping`（随父开关，默认日探）
 经 watchdog `_check_pool_key_pings` 对池 key 打 1-token chat ping——「被封/装错端点/模型名失效」
 只有真打才暴露；端点通但拒绝（401/4xx/5xx）→ key 失效告警（6h 去抖），网络不可达静默。
 纯函数 `chat_ping_targets`/`run_chat_pings`（节流+状态快照 `ping_state_snapshot`）。
 管理面：`GET /api/setup/cloud-credentials`（余额+探活+池运行态汇总，密钥全程掩码，`?probe=1`
 强制重探）+ `POST /api/setup/key-pool`（写 overlay `ai.key_pool` + `reload_ai_runtime` 热生效；
 掩码/空 key 回传自动沿用同名旧真值，重名/超10个/新条目空 key 拒绝）；UI 在 `/developer` 页
 「☁️ 云端凭证体检 / 备用 Key 池」卡（添加/删除/立即体检/保存热生效；删除按钮走事件委托）。
 `AIClient.pool_status()` 出冷却态快照（无密钥）。i18n `dv_cc_*`/`err.setup.pool_*` zh+en 齐备。
 门禁 `tests/test_key_pool_routes.py` + watchdog 探活用例（`test_cloud_outage_alert.py`）。
 **池智能排序 + 坐席降级条 + 出话分布**（同日傍晚续）：`order_pool_entries`（纯函数）按
 冷却态→探活档位（ok<无数据<fail）→运行态新鲜度（last_ok_ts）→探活延迟→配置序排池，
 主 Key 挂时第一击命中好钥匙（ping 快照 fail-open）；三条出话链各记 `_last_*_ok_ts` →
 `degradation_snapshot()`（只认正面证据：熔断开路 或 主链停+池/本地在顶班；无流量≠降级）
 → `GET /api/workspace/ai-runtime-status`（任意登录坐席可读，无敏感字段）→ workspace_base
 降级状态条（60s 轮询，恢复自动消失；桌面壳包同一工作台=桌面端零代码同享；接口连挂 2 次
 只收条不误报）。「出话分布」（主链/备用/本地 calls+cost，llm_cost 按 tier 聚合）进
 `/api/setup/cloud-credentials.usage` + developer 凭证卡 📊 行。i18n `ws.aidegrade.*`/
 `dv_cc_js_u*` zh+en。门禁：排序/降级快照/路由/静态 wiring 用例齐备。
- **命理技能**（`companion.bazi`，2026-07-12，对标 AuraMate「灵体对话」的接入式复刻 Phase 1）：
 聊到算命/运势 → 排盘结果作**内部参考**注入 prompt（`user_context["_bazi_block"]` →
 `_build_context_prompt` 消费），LLM 以当前人设口吻自然展开——**注入而非短路**（不切报告腔）。
 排盘 `src/companion/bazi_engine.py`＝lunar_python 薄封装纯函数（四柱/十神/藏干/纳音/五行
 月令双计/强弱粗判+喜用候选/大运/流年；立春分年；进程级缓存），**单一事实源**防「各处喜忌
 口径不一致」（AuraMate v1.0.1 踩过）；缺库/非法输入软失败 None，聊天零阻断。**诚实边界**：
 时辰未知不出时柱、性别未知不排大运（阳男阴女顺逆，瞎猜错一半）。生辰抽取
 `bazi_profile.py`（关键词门控+多格式+传统时辰/段落词小时+农历 flag+性别；「晚上12点」跨日
 柱歧义宁缺勿错；**第三人称护栏**——「我男朋友1993年…」不落本人画像防排错盘）；落库为
 episodic `user_stated` 规范事实（与生日 Stage S 同机制：用户原话或 AI 复述确认触发
 `_capture_birth_info_fact`，幂等去重+性别继承；话题窗内「我是女生」补录解锁大运）。
 对话层 `bazi_context.py`：话题检测（保守多语 marker+否定护栏）+ **粘性窗**（默认 10min，
 「那我明年呢」类无关键词追问持续注入）+ 缺生辰顺势采集 directive（24h 冷却防逼问；已知
 生日 (月,日) 时体现「记得你生日」只补年份时辰）+ **同轮闭环**（消息自带生辰当轮排盘不反问）。
 注入块自带安全红线（不预言死亡/重病/灾祸时点、重大决策仅参考视角、低落先共情——衔接
 既有 crisis safety net）。两条链路都接：process_message（story block 之后）+
 generate_inbox_draft（3c，`_metric("bazi_active")` 可观测）。默认 enabled:false；
 依赖 lunar_python 已进 requirements（+CI）。门禁 `tests/test_bazi_engine.py`（金标命例
 乙亥/戊寅/乙未/庚辰 + 立春边界 + 农历直排 + 大运方向 + 结构不变量）/
 `test_bazi_profile.py` / `test_bazi_wiring.py`。
 **Phase 2**（同日，留存钩子+付费实验闭环）：① **每日灵签** `src/companion/bazi_daily.py`
 ——签面=今日干支的真实日运信号（有生辰：今日日干对TA日主的十神→受助/表达/同频/务实/
 收敛五类能量日，千人千面且当天恒定可验证；无生辰：按今日五行通用签），宜忌/幸运色按
 crc32(日期+用户) 确定性轮转，**吉凶零断言**（无「大凶」恐吓词，门禁扫池子）；聊天入口
 `detect_daily_card_intent`（「今日运势/抽签」，纯问签不逼生辰）+ **晨安 ritual 顺手翻签**
 （`_ritual_daily_card_line`：仅 bazi 开+`daily_card_in_ritual`+**已给过生辰**的用户+非 soft
 情绪档才附一句，零骚扰，软失败不阻断问候）。② **详批变现**：`detect_deep_reading_intent`
 （详批/事业运/财运/感情运…）→ `_bazi_deep_allowed`（selfie 同款权益链：
 `user_context.entitlement`→`feature_allowed`，monetization gate 总闸关=恒放行零破坏）→
 放行=详批指令+**大运序列真数据**进盘面；拦下=免费大方向+软引导（`upsell_offer`→
 `upsell_pitch_hint` 带目录报价，**绝不硬拒**），**指令与数据同口径**（未解锁不给详批级
 大运数据）。目录新增 items.bazi_reading（$4.99，config 可覆盖），配置 `premium_feature`。
 埋点 `_record_bazi_funnel`（复用 funnel teaser 事件：bazi_deep/bazi_upsell）。
 ③ **流年真数据**：`extract_target_year`（今年/明年/后年/20XX，生年防误判）→
 `liunian_detail`+`format_liunian_line` 把所问年份干支+对日主十神喂给 LLM——防 LLM
 徒手编干支（命理场景最常见事实性硬伤）；`shishen_between` 纯函数十神表与 lunar_python
 金标交叉验证。门禁 `test_bazi_daily.py` + `test_bazi_engine.py`/`test_bazi_wiring.py` 扩容。
 **Phase 3**（同日晚，读数看板+晒图传播）：① **观测卡**：`src/companion/bazi_stats.py`
 （进程级单例，风格对齐 avatar_voice_stats；漏斗四段=话题触达→生辰采集→灵签/命盘供给→
 详批变现，含同轮出盘/性别补录/K线成败/capture_rate）→ `/api/workspace/metrics.bazi` +
 Prometheus `bazi_*`（drafts_routes 两处并入）→ ops-overview「🔮 命理技能」卡
 （`ov2_s_bazi`/`ov2_bz_*` zh+en；`active=false`（零流量）整卡隐藏）。埋点接在注入/
 capture/ritual/详批门控/K线发送各点，全部 best-effort。② **人生 K 线卡片**
 `src/companion/bazi_kline.py`：逐年评分纯函数（基准50 + 流年干/支五行×命局喜忌 ±16/±10 +
 所处大运 ±8/±5 + 十神微调 ±2，夹 8..92——**确定性可解释，不是玄学随机数**；中和命局
 喜忌分量为 0 → 曲线平缓=诚实）+ PIL 深底曲线卡（1080×640，大运分段底色+逐年干支标注，
 CJK 字体缺失自动退化 ASCII 不崩）；Stage C `_handle_bazi_kline_request`（selfie 同款
 三态短路：""=图已发/None=非请求或缺生辰回落）经 `_try_send_selfie_media` 双路发图，
 出图目录 `tmp_bazi`（/tmp_* 已 gitignore）。**图片免费**（分享传播面）、逐年详解文本仍走
 详批门控——图引流、深度变现；缺生辰 → 回落注入路径顺势采集。门禁 `test_bazi_stats.py`
 （计数/导出/metrics 路由端到端）+ `test_bazi_kline.py`（喜忌年份分差≥20/确定性/夹界/
 中和平缓/大运定位/PNG 渲染/Stage 三态/同轮闭环）。
 **Phase 4**（2026-07-13 凌晨，多平台出图+灰度开闸）：① **K 线进 autosend 链**：
 `image_autosend.run_autosend_kline`（**独立于 companion.selfie 开关**，gated 于
 companion.bazi.enabled+kline；同轮生辰 or 注入的 `resolve_birth` 回调 → 渲染 →
 `save_outbound_media` → send_fn）+ 胶水 `autosend_helpers.autosend_bazi_kline`
 （orch.owns_media 反双发 + inbox 最近入站文本 + web loop marshalling + 与草稿注入
 同口径记忆键 `_episodic_storage_key(chat_key,"",platform)`）；挂在 `_autosend_deliver`
 **最前**（求曲线是最具体的结构化意图，先于泛化要图）；缺生辰 → False 回落正常草稿流
 （注入路径顺势要生辰）。成败计入 bazi_stats.kline + image fallback 原因
 （kline_render/stage/deliver_failed）。门禁 `tests/test_bazi_kline_autosend.py`。
 ② **灰度开闸**（本机 overlay）：`companion.bazi.enabled: true`——反应式全开（话题/
 采集/灵签/K线），**monetization gate 保持关**（第一周纯免费读触达/采集/灵签数据，
 付费实验看数据再启）；晨安灵签行依赖 proactive_topic.daily_ritual（当前关，开 ritual
 时自动生效）。攒批重启含本日全部改动；开闸后看 ops-overview「🔮 命理技能」卡读数。
 **Phase 5**（同日凌晨，质量门禁+付费配方+知识备货，全部不依赖流量数据）：
 ① **命盘质量评测** `src/eval/bazi_chart_eval.py`（确定性四轨，缺 lunar_python 优雅跳过）：
 四柱金标回归钉（8 例含公开史料锚点：1949-10-01 甲子日/2000-01-01 戊午日；升级
 lunar_python 漂移立刻点名）+ 十神双实现全盘交叉验证（日期扫描 ~345 盘 shishen_between
 vs getShiShenGan）+ 强弱/喜用一致性不变量（比例边界外判词不得矛盾、判词↔喜用映射
 不得漂移、五行计数守恒=9）+ K 线评分健全性。CLI `python -m scripts.run_eval --bazi
 [--json]`；门禁 `tests/test_bazi_chart_eval.py`（含**探测器有效性自证**——篡改金标必
 FAIL，评测不是摆设）。② **付费实验配方端到端验证** `tests/test_bazi_paid_path.py`：
 grant 路由（`POST /api/monetize/grant {contact_key,kind:unlock,item_id:bazi_reading}`）
 → EntitlementStore → `set_relationship_providers(entitlement_resolver)`（bootstrap 同款）
 → `resolve_entitlement` → `_bazi_deep_allowed` 放行；幂等 ref 防重复入账；vip 默认
 **不含** bazi_reading（详批只走单点解锁；会员含详批需 catalog overlay 给 tier 加
 grant——两条路都已验证）；订阅到期权益自动失效。开付费实验的操作配方＝
 overlay 开 `monetization.enabled`+`monetization.gate.enabled` → 重启（bootstrap 注册
 resolver）→ grant 路由发放测试号。③ **KB 命理起步包**：`kb_starter._STARTER_PACKS`
 新增 `bazi` 包（16 条：八字/十神/强弱/喜用/大运/流年/时辰/农历公历大白话 + 「问生死
 病灾/赌博财运」安全话术 + 反推销改运口径；独立「命理」分类，conversion 域
 categories.yaml 已登记），运营在 `/api/kb/seed-pack` 一键播种（需下次重启后可见新包；
 播种后建议 embed-all）。门禁 `tests/test_kb_starter.py::test_bazi_pack_safety_and_coverage`
 （高危话术必含就医引导/不背书赌博 + BM25 可检索）。
 **Phase 6**（同日拂晓，LLM 解读质量轨+投产收尾）：① **LLM 解读质量评测**
 `src/eval/bazi_reading_eval.py`——命理场景三类确定性可检的 LLM 质量事故：**干支幻觉**
 （提到盘里不存在的干支，`chart_ganzhi_universe`＝四柱∪大运∪窗口流年 now-2..+12 为合法集）、
 **失地**（整段没引用任何盘面事实=等于没排盘）、**宿命断言红线**（必死/必离/血光之灾
 恐吓式预言，与 crisis 红线正交）。校验器纯函数常驻门禁 `tests/test_bazi_reading_eval.py`
 （含好/坏假 LLM 编排验证 + LLM 异常不得装作通过）；真 LLM 实跑 opt-in：
 `EVAL_LLM=1 python -m scripts.run_eval --bazi-reading`。2026-07-13 首跑基线：
 DeepSeek 9 样本（3 命例×3 问法）**9/9 合格，零幻觉零红线**——注入盘面数据后大模型
 不编干支的假设成立。校验器将来可平移为线上出站守卫（Phase 7 候选）。
 ② **投产收尾**：攒批重启装载 Phase 5/6 代码 → `/api/kb/seed-pack {domain:bazi}` 播种
 16 条命理包 → `/api/kb/embed-all` 向量化 →「命理」分类在 `/knowledge` 可管理。
 Phase 7 backlog：付费实验开闸（读数后）、合盘/他人档案、解读校验器上线做出站守卫。

### 主动触达 Phase13（2026-07-13，全自动回复 + 每隔几小时主动打招呼/发语音）

**运营方针**：好友消息全自动回复不停审核 + 按聊天进度每隔几小时主动打招呼、发语音。

**开闸清单**（overlay `config.local.yaml`）：`companion.proactive_topic` enabled（沉默≥4h
主动、同会话冷却 6h、每 tick(15min) ≤3 人、安静时段 23-8 不发、`voice.probability: 0.5`
克隆声语音开场；**platforms: []=全编排器平台私聊**）+ `daily_ritual` enabled（晨 7-10/晚 21-24 按用户活跃点个性化择时，
min_intimacy 10；晨安灵签行随之激活）+ Messenger review 封顶解除（`platform_modes: {}`）。
`inbox.auto_draft` **键名修正** + **`bootstrap_automation_mode: true`**：首条入站自动持久化
`auto_ai`（修 store 默认 review 与 autodraft 全局 auto_ai 口径分裂 → UI/让位/System Z 一致）。

**主动语音开场**（`proactive_topic.py::_try_send_voice`）：`voice_gate` 纯函数
（开关+长度带 4..80+概率 0.5，混发比全语音更像真人）→ 语言错配护栏（会话语言非中文
→ 文本，克隆声念不了外语）→ `stage_voice_file`（预渲染命中零 GPU/7852 混合保真）→
`orch.send_media(inbox_text=念稿)`；人设灰度名单与 `l2_autosend.voice.persona_allowlist`
同口径；任何失败回落文本，绝不丢触达。非中文会话的开场文案本身经
`build_proactive_prompt(peer_language=)` 硬性要求用对方语言写（修真机：给全程英文
客户发了中文开场）。

**四个真机踩坑（都有门禁）**：① planner 传 `last_emotion_intensity` 而 `_opener`
包装缺参 → TypeError 逐会话吞掉 → 候选恒 0（`test_planner_opener_kwargs_contract`
三件套钉签名契约）；② 候选没过滤会话类型/账号 → 给群聊发"想你了"+ 用主账号发别人
账号的会话 → **A 线对不认识的 peer 发送 → PEER_ID_INVALID → ban_signal 误判风控 →
kill-switch 冻结主账号 1h**（修复：只面向「能发的账号×私聊」+ 回落主客户端仅限
default 账号会话）；③ **跨 loop**：worker 的 pyrogram client 活在 web 线程 loop，
从主 loop 直接 `await orch.send` → "attached to a different loop"（修复：
`_run_on_web_loop` marshalling，与 autosend_helpers 同口径）；④ 对方已注销
（INPUT_USER_DEACTIVATED）/peer 解析不了 → 发送失败不记冷却 → 按沉默降序永远
排前占满每 tick 名额（修复：进程内 `_bad_peers` 拉黑集）。首轮真发实证：
`[proactive] 语音开场已发 telegram:8244899900 chat=8921664288 mode=follow_up`
（follow_up 引用记忆"混血儿/旅行"，冷却已记）。观测：`/api/companion/proactive/preview`、
`/api/drafts/autosend-status` 的 `proactive_topic` / `automation_bootstrap` 段。

### 主动触达 Phase14（2026-07-13，关系深度自适应节奏 + 观测）

**`adaptive_pacing`**（`src/utils/proactive_pacing.py`）：亲密度 0→100 线性插值
`min_silent_hours` / `cooldown_hours`——新好友（intimacy≈0）等 8h/10h 才主动，熟客（≈100）
2h/4h 即可问候；`plan_proactive_sends` 逐会话生效并写入 plan 的
`effective_min_silent_hours`（发送前活跃复核同口径）。config.local 已开。

**观测**：`automation_mode_stats`（bootstrap 累计）、`proactive_stats`（tick planned/sent）
挂入 `/api/drafts/autosend-status`。

### 主动触达 Phase15（2026-07-13，双轴 pacing + 外语语音 + ops 卡）

**双轴 pacing**：`combined_pacing_score = max(intimacy, stage_score)`（warming=28、
steady=52、intimate=78…）→ 聊很多轮但 intimacy 分低也能更早主动；发送前复核同口径。

**外语主动语音**（`proactive_voice_foreign.py`）：非中文会话 + `voice.foreign.enabled`
→ edge 多语神经声念外文稿（不占 7852）；中文仍克隆声。config.local 已开
`languages: [en, ja, ko, th, vi, id, es, hi]`。

**ops-overview 卡**：`GET /api/companion/proactive/status` + `loadProactiveEngagement()`
（tick/语音/bootstrap/候选/双轴/外语开关 + **Top5 pacing 样本表**）。观测亦在 autosend-status。

### 主动触达 Phase16（2026-07-13，ops 样本表 + LINE 接受后欢迎）

**ops 卡增强**：`preview.sample` 渲染 stage/intimacy/silent_hours/effective_min_silent_hours。

**LINE friend_welcome**（`friend_welcome.py` + `find_friend_accept_rows`）：
接受好友 → 入队 companion 问候（send_queue 投递）；`line_rpa.auto_accept.welcome.enabled`；
幂等 `line_rpa_meta friend_welcome:{peer}`；接受后 `_trigger_evt` 加速投递。

### i18n 施工约定（后台路由 CJK 收口 + 前端裸键）

**词条单源规则（P4 起，先读这条）**：
- **新增词条一律进 `src/web/i18n_packs/<域>.py`**（每个 pack 模块暴露 `ZH`/`EN` 两个 dict，键一一对应），
  **禁止再往 `web_i18n.py` 的 `_TRANSLATIONS` 单体字面量加键**——16k 行单体曾多次发生并行编辑互相
  覆盖的丢失更新，packs 让各工作流各改各的文件。冲突有门禁：pack 之间同 key 加载即抛错；
  pack×单体同 key 由 `test_i18n_packs_bilingual_and_no_collision` 拦截（"双源真相"禁止）。
- 消费方全部透明：`get_translations()/t()/tr()` 返回"单体+packs"合并视图，模板 `(i18n or {}).get`、
  JS `window.T`、路由 `tr()` 无需感知来源；改 pack 文件热更新即时生效（`_maybe_reload` 同时监视
  pack 目录 mtime，且 pack-only 变更不重新 exec 单体，比改单体更快）。
- 存量键从单体搬家用 `tools/i18n_migrate_domain.py --prefixes <前缀> --pack <域名>`
  （单行词条提取 / 双语齐平校验 / pack 冲突中止 / **迁移前后合并视图逐键一致否则自动回滚**）。
  七域 4375 键已迁（messenger/telegram/line/whatsapp/rpa_overview/ops_overview/errors/inbox/persona）。
- 同族单源：**侧栏导航**改 `src/web/nav_schema.py`（简洁/完整两模式+命令面板+渠道状态点全部由它驱动，
  勿在 `base.html` 手写 `<a>`）；**悬浮词典**改 `src/web/help_terms.py`（base.html 经
  `help_terms|tojson` 消费）。

后台 API 的 `detail`/`error` 文案前端 verbatim 直显 → 硬编码中文会漏给英文用户。收口靠请求级
`tr(request, key, default=None, /, **fmt)`（`src/web/web_i18n.py`），从 `request.state.ui_lang`
取语言出译文。**所有后台 routes 现已收口至 0 CJK**，靠 ratchet 门禁 `test_route_response_cjk_ledger_ratchet`
（`_ROUTE_CJK_CEILINGS` 为**非增天花板**，新增硬编码中文即红）守住。

**新路由族批量收口标准流程**（工具 `scripts/i18n_routeconv.py`）：
1. `python -m scripts.i18n_routeconv --coverage-all` 选靶（ratio=1.0 可一把过；<1.0 差集是硬骨头）。
2. `--suggest ROUTE_FILE` 出键匹配建议（reuse 现有键 / new 新键）；优先复用 `err.svc.*` / `err.rpa.*` /
   `err.ws.field_required` 等共享词汇，参数化（`{field}`/`{name}`/`{dep}`）而非造同义新键。
3. driver 里 **`convert_file` 的 `scope_check` 现为缺省 `True`**（P43e；勿轻易传 `False`）：施工**前**跑
   `scope_precheck` 剔除落在无 `request` 作用域的映射（不动源码、列 `scope_skipped`），从源头杜绝
   `tr(request,…)` 写进无 `request` 的 helper 而运行时 `NameError`。**任何 helper 若要调 `tr` 必须把
   `request` 收进形参**并改所有调用点。
4. 事后 `--verify-scope ROUTE_FILE` 复核（应空）；在 `src/web/i18n_packs/errors.py`（或对应域 pack）
   补齐 zh/en 两套键——**不要**加进 web_i18n.py 单体（见上方"词条单源规则"）。

**两条硬护栏**（勿踩）：
- 占位符名**不得**叫 `request`/`key`/`default`（会与 `tr` 形参撞名；`tr` 已用位置限定 `/` 兜底，
  另有门禁 `test_i18n_placeholders_avoid_reserved_names` 从源头禁用）。历史坑：`err.rpa.config_missing`
  曾用 `{key}` → 改 `{name}`。
- 新键必须 zh+en 双语补全。前端 `window.T('key')` **不留中文兜底**（回落只显裸键名）；每个**静态**
  `window.T`/`Tf` 键必须在 web_i18n.py 存在，由全库门禁 `test_template_window_t_keys_resolve`
  （`templates/**/*.html` 递归 63 页，零缺失）守住。CLI 键覆盖自检见 `tests/test_i18n_coverage.py`。

### Feature flag 约定

- 新子系统默认 `enabled: false`（见 `config/config.yaml::contacts.enabled`）
- ALTER TABLE 集中到 `src/**/database.py` 的 migration 列表，不散落

### 生产服务重启纪律（本机 main.py 常驻，重启窗口 ~15-30s 全站不可用）

**能不重启就不重启**：
- 纯模板改动（`src/web/templates/**.html` 的 HTML/JS）**免重启**——Jinja2 `auto_reload`
  已开（`src/web/admin.py`），刷新浏览器即生效；
- `web_i18n.py` 键改动**免重启**——mtime 热加载（2s 节流，坏保存态保留旧字典）；
- `config.yaml` / `config.local.yaml` 改动**免重启**——`check_and_hot_reload` 双文件监视
  （30s 节流；重载走 load() 同路径保 overlay 不丢；telegram 凭证键受保护不热改），
  触发点＝Telegram 消息循环 + **web 请求检查点**（静默期靠任意页面/接口访问触发）；
  brand 白标段随 on_reload 联动刷进模板 globals；
- 验证类改动先跑 pytest（测试自建 app/store，不依赖常驻服务），别用「重启生产看效果」当测试。

**必须重启时**（业务 .py 改动）：
- 用 `scripts\restart_main.ps1`（停旧→起新→**轮询 /login 到 200**→报告窗口耗时；
  起失败会大声报错而非静默死机）；
- **攒批重启**：多项改动合一次重启，别每改一行重启一次（2026-07-12 曾一天重启 13 次，
  坐席端反复撞「加载超时」红屏——前端虽已有自动退避重连自愈，但窗口本身应尽量少出现）；
- 多 agent 并发**必须 worktree 隔离**（见 Git workflow），只有负责生产机的那条线才碰常驻服务。

### Git workflow

本 repo **2026-04-24 首次进 git**。现阶段：
- `main` 为主分支；baseline 见 `git log`（初次 import + AGENTS.md + gitignore 强化）
- 后续 feature 走 `feat-*` 分支 + PR（参考 `mobile-auto0423` 的 squash merge 流程）
- **多 agent 并发用 `git worktree` 隔离**：普通分支只隔离提交历史，多个 agent 仍共用
  同一工作目录 → 文件互相串改、`index.lock` 互撞（本仓曾反复踩）。各 agent 各开
  `git worktree add -b feat-xxx ../telegram-mtproto-ai-xxx <base>`（独立工作目录 +
  独立 index、共用 .git refs），冲突只在 merge 时显式解决；收尾 `git worktree remove`。

### 崩溃恢复提示

- 本项目不在 git 之前的工作记录在 `DEPLOYMENT_STATUS.md` / `TODO_NEXT.md` / `docs/` 下多份 `*_PLAN.md` 与早期分析（历史文档，可能已过期，**以代码为准**）
- 已知含**虚构 model ID** `Codex-4.6-oups-high` 的 deprecated docs（不要被这些占位误导）：`CURSOR_DEVELOPMENT_GUIDE.md`、`CURSOR_HANDOFF.md`、`docs/MONITORING_PLAN.md`、`docs/MONITORING_API_SPEC.md`、`docs/ORDER_REPLY_GENERATION_ANALYSIS.md`、`docs/LOG_ANALYSIS_OPTIMIZATIONS.md`——本 repo 实际 ai provider 见 `README.md` + `config/config.yaml::ai`
- `~/.Codex/projects/C--telegram-mtproto-ai/memory/` 里 `MEMORY.md` 按项目分组，本项目条目见 "Project: telegram-mtproto-ai" 段
- 关键教训：`project_tasklist_drift.md` — 文档落后于代码，重入时以 `grep` 验证代码实况再信任任务列表

## 不在本 repo 范围（见 PROJECT_SCOPE.md）

Facebook add_friend / greeting / auto_reply / VLM Level 4 fallback 栈 → `github.com/victor2025PH/mobile-auto0423`
