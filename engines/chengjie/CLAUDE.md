# telegram-mtproto-ai — Claude Code 项目指令

> 本仓库是多平台 AI 客服的主骨架。Claude Code 在本 cwd 启动时自动加载本文件。
> **边界声明**见 [`docs/PROJECT_SCOPE.md`](docs/PROJECT_SCOPE.md)（权威文档）。

## 仓库一句话

`main.py` 启 FastAPI，内嵌：contacts/handoff 子系统 + Telegram/LINE/Messenger 三端 RPA runner + skill_manager / KB / 回复生成 / 语言守卫 + Web 后台 + observability。

## 四平台能力差异（别再通读四个 worker 源码）

被问「telegram / whatsapp / line / messenger 的聊天逻辑和能力一不一样」时，答案在
[`docs/平台能力矩阵.md`](docs/平台能力矩阵.md)——由 `scripts/platform_matrix.py` **从代码
生成**，门禁 `tests/test_platform_matrix.py` 钉住与代码一致，**勿手改**（改了 worker 就重跑
`python -m scripts.platform_matrix --out docs/平台能力矩阵.md`）。

- **回复生成逻辑四平台共用**（A 线 `process_message` / B 线 `generate_inbox_draft`，按会话
  automation_mode 互斥切换，与平台无关）；差异只在**收发管道能力**。
- 看本机实况 + 核对「代码说支持的、实际到货了吗」：
  `python -m scripts.platform_matrix --with-live-data`
- 出站记账口径专项（媒体发了却只记成文字占位）：`python tools/check_outbound_media_mirror.py`
- LINE 媒体链路真机探针（默认只读，`--confirm` 才发且只发给自己）：`python tools/probe_line_media.py`

⚠ **别信散落在 worker 里的能力注释**：2026-07-31 实测有三处是错的或已过期（`mark_read`
的 docstring 写「WA 暂无」而 WhatsApp 早就实现了）。以矩阵为准。

## Claude 在本 repo 工作时的约定

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

**仅 contacts/handoff 主线**（快速回归）：
```bash
python -m pytest tests/test_contacts_*.py tests/test_gateway_*.py \
  tests/test_account_limiter.py tests/test_handoff_readiness.py \
  tests/test_intimacy_engine.py tests/test_reactivation_scheduler.py \
  tests/test_handoff_*.py tests/test_cap_alert.py \
  tests/test_rpa_contact_hooks_wireup.py tests/test_contacts_runner_bridge.py \
  -q --tb=line
```
预期：全绿（contacts/handoff 主线子集，含 runner→真 hooks→store bridge 测试）。

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
python -m pytest tests/test_inbox_inline_handlers_exported.py tests/test_rpa_inline_handlers_exposed.py tests/test_template_unique_ids.py tests/test_template_orphan_refs.py tests/test_template_dynamic_dot_access.py tests/test_template_free_capture.py tests/test_static_js_free_capture.py -q --tb=line
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

**工作目标「建目标弹层」草稿幸存 + 编辑防打断主线**（2026-08-04/05 P0-P3，修坐席实录
「弹层误触即丢内容 / 没动鼠标编辑中也消失 / 丢了无法恢复」三连）：
```bash
python -m pytest tests/test_goal_ui_revamp.py -q --tb=line
python tools/verify_goal_form_ui.py   # fixture 真浏览器 22 场景（零实例依赖/零遥测污染）
```
预期：全绿。关键不变量（改 cp-goal.js / cp-panel-base.js / syncWsCopilot 前先读）：
① 第二步弹层输入逐键快照 sessionStorage（键=会话 id，24h TTL），任何整块重渲染后
 `_applyFormDraft` 原样回填；创建成功/「清空重填」才清；重开表单断点续写直跳第二步。
② **编辑期同会话的宿主 context 重喂一律挂起**（cp-goal `set context` 覆写，关表单补刷）；
 宿主 `syncWsCopilot` 同 cid 一律不重喂右栏组件（轮询身份合并/peer 解析回调/回前台补轮
 是三大核爆源；内联组件不消费昵称/头像，同会话重喂＝纯破坏；App 模式按身份指纹判）。
③ 退出语义：背板判脏（有输入不关闭只提示已暂存）；×＝关闭整表单；「返回」/Esc＝回第一步。
④ renderData 的 error/active 分支不得顶掉打开中的表单（冲突交后端 active_limit 409 就地报错）。
⑤ 基类 `CpPanelBase.refresh` 同会话刷新不清屏（换会话仍先清屏防串数据；无旧数据保 loading 反馈）。
⑥ 发布纪律：改共享组件必须 bump `?v=`（unified_inbox.html + shared/copilot/app.html 两处）
 + ui-build.txt + desktop/renderer 镜像同步（test_goal_ui_revamp 钉版本戳前缀，双树门禁钉同步）。
观测：ops-overview 🎯 卡「🛟 表单体验」行＝进程口径 + 14 天趋势（`/api/admin/ui-event-trend
?prefix=goal_`，zhiliao 已开 ops.ui_event_trend）；核心埋点 goal_ctx_deferred（编辑被外部刷新
打断-已挡）/ goal_form_backdrop_dirty（背板误触-已拦）/ goal_form_draft_restore（断点续写）。

**右栏「语音克隆/发送」cp-voice 状态机主线**（2026-08-05 P0，修坐席实录「生成的试听
清不掉 / 想再来一条找不到生成按钮」两连报障 + 连点双发隐患）：
```bash
python -m pytest tests/test_cp_voice_ui_revamp.py -q --tb=line
python tools/verify_cp_voice_ui.py   # fixture 真浏览器 27 断言（stub client：零实例依赖/零 TTS 消耗）
```
预期：全绿。关键不变量（改 cp-voice.js 前先读）：
① 生成按钮动词化（`cp.voice.tts_btn`＝「🎙️ 生成语音」）+ 请求期 busy 互斥（`_setBusy`，
 连点＝双烧 GPU/给客户双发）；预览区常驻「🔁 重新生成 / ✕ 清除」，**错误态同样带重试/✕**
 （错误文案赖着不走与旧预览赖着不走是同一个病）。
② 发送成功即复位（清预览+清文字+「已发送」驻留后自动恢复引导语）——与主输入框
 `sendVoiceReply` 行为对齐；发送带幂等键 `client_msg_id=cpv-*`（`send_dedup.reserve`
 对空键直接放行，右栏此前裸奔；主输入框链早已带键）。
③ 过期守卫：文字/音色偏离生成基准（`_previewText`/`_previewPersona`）→ 预览标 stale +
 发送禁用（发送是服务端按**当前**文本重新合成，不拦＝发出从未试听过的内容）；
 cp-fill 程序化填入不触发 input 事件，组件内已补 `_syncStale()`。
④ `_epoch` 渲染代际：在途请求跨会话切换一律作废（防「已发送/预览」串会话回写）。
⑤ 宿主消费 `cp-voice-sent`（unified_inbox `__cpVoiceSentBound`）→ toast + loadThread +
 loadChats，修「发完消息流不动，坐席以为没发出去再点一次」（此前事件发了没人听）。
⑥ 回落警示：`voice_meta.fallback_from` 非空 → 黄字「标准音色（非人设克隆声）」——克隆链
 掉线时试听/发出的不是人设的声音，对客户是「换人了」级破绽，必须显式而非灰字黑话。
发布纪律照旧（双树镜像 + `?v=` 两处 + ui-build.txt，本批 `20260805c`）；浏览器门禁已挂
`gate_sweep -Full`。与同日并行线的「试听带会话上下文（试听=发送契约）」「`__system__`
系统音色三档」互补共存，双方契约由 `test_cp_voice_ui_revamp` 一并钉住。
**P1 增量（同日，所听即所发闭环）**：
```bash
python -m pytest tests/test_tts_preview_reuse.py -q --tb=line   # 契约 18 例
```
⑦ **试听产物复用**：tts-test 落盘时写 `<音频名>.json` sidecar（文本 sha1+音色键+
 元数据，`src/integrations/shared/tts_preview.py::record_preview_meta`）；send-voice
 带 `preview_filename` 且校验通过（同文本指纹/同音色键/未过期/非空壳/文件名白名单
 防穿越）→ **复制后**把试听音频直接送出站管线（原件保留，发送失败重试仍可用）——
 客户听到的与坐席试听的逐字节一致，且省一次合成/字符额度；任何校验不过回落现场
 合成（`resolve_reusable_preview` 返回 miss 原因进 INFO 日志）。复用分支**刻意跳过**
 时长质量闸门（坐席耳朵已把关）；响应带 `reused_preview`，宿主埋点 `cpv_sent_reuse`
 分桶。persona_key 用**请求侧** persona_id（含空串），依赖同并行线的「试听=发送
 同入参解析」契约。
⑧ **取消生成**＝`_cancelGen`：epoch 代际+1 复用「切会话作废」同一机制——零服务端
 桥接、桌面壳零改动，在途结果回来即静默丢弃；**计秒 tick 只更新 span 不整块重写**
 （否则取消按钮每秒被销毁重建，点不中）。
⑨ **字数计 400**（`_syncCounter`，与 tts-test 上限同口径）：超限红字+生成禁用+
 前端先拦；`_setBusy` 解除 busy 时保留超限禁用（否则清 busy 会点亮超限按钮）。
⑩ **复用观测三暴露面**（P2 同日）：计数器长在契约模块（`tts_preview.reuse_stats_snapshot`
 ——recorded/hits/misses{原因}/hit_rate，resolve 入口自动计数）→ `/api/voice/avatar-status.
 preview_reuse` + `/api/workspace/metrics.voice_preview_reuse` + ops「🎙️ AvatarHub 语音」卡
 「所听即所发」行（`ov2_av_reuse*`，零流量不占版面）。调参读法：misses 里 expired 多
 → 放宽 REUSE_MAX_AGE_SEC；text_mismatch 多 → 坐席改稿没重生成，强化前端引导。
⑪ **两个数据否决的「刻意不做」**（2026-08-05 生产日志实测，~52MB 窗口）：语音流量
 主力=A 线 TG 原生 voice_reply（有 prerender 命中层，单次合成无浪费）；**B 线 autosend
 语音≈0、坐席手动 send-voice=0** ⇒ B 线复用接线无数据支撑不做；composer 内联语音路径
 合流（genVoiceReply/sendVoiceReply 接 preview_filename）**服务端已就绪、纯前端接线**，
 但该链当前零流量 + unified_inbox.html 属多线活跃热区，等右栏语音真实用量起来后再合流。
⑫ **线上冒烟工具** `python tools/smoke_voice_reuse.py`（2026-08-05 已实弹验证 8/8：
 S1 观测装载 → S1.5 自动发现可发语音的协议账号（'default' 不是受管账号，owns_media
 要确切 id）→ S2 试听+sidecar → S3 复用发送 reused_preview=true → S4 计数+1 →
 S5 收藏消息镜像）。**只发账号自己的收藏消息**（chat_key 钉死 'me'）；`--dry-run`
 零发送零合成。**刻意不进计划任务/gate_sweep**（每周真发=收藏消息积杂物，与 multiwin
 周批同决策：被动观测走 ops 卡计数）。实测复用命中 `dur=0ms`（发送侧零合成延迟）。
 附带教训：改 `.py` 后先看重启冷却账本（`-Advise` 显示 last restart）——本批代码被
 并行线 10:48 的重启顺路装载，读探针验证即可，不必再吃一次重启窗口。

**坐席语音手动链「所打即所念 / 所听即所发」主线**（2026-08-10/11，修实录「手打
『你晚上吃饭了吗，晚上有什么安排』，语音念出的是**对它的回答**」+「11 字 40 秒」）：
```bash
python -m pytest tests/test_voice_send_verbatim.py tests/test_voice_send_status.py \
 tests/test_voice_colloquial_llm.py tests/test_voice_content_preservation.py -q --tb=line
```
预期：全绿。关键不变量（改 send-voice / tts-test / voice_colloquial_llm / 收件箱
composer 语音区前先读）：
① **手动链原文直念**：send-voice 与 tts-test 的 synthesize 必传
 `pre_colloquialized=True + interactive=True + total_budget_sec`（试听=发送**全同参**，
 试听产物经 preview_filename 复用为出站音频时零分叉）；interactive 同时把 hub 候选
 封顶 1（synth_verify 已兜坏 take，第二候选对交互路径是纯延迟税）并**豁免开场词
 去重剥词**——手打文字一个字不动；TTS 缓存键含改写变体维度（verbatim 与改写链
 同文本不是同一份音频，不分键会跨链串播旧「改过词」音频）。
② **LLM 口语化只属自动链兜底**，三道枷锁（2026-08-10 答话式漏网后加）：问句主导
 跳过 LLM 档（`is_interrogative_dominant` 子句级判定——「把问句答掉」是小模型最高发
 翻车，答话与原问同话题余弦天然偏高，语义守卫拦不稳）；语义地板 0.78→0.84（当日
 生产漂移已爬到 0.746/0.774）；语义校验不成立＝**fail-closed** 拒用 LLM 稿（不投毒
 缓存不推熔断——嵌入侧故障不冤枉改写端点）。`guard_stats` 计数经 health_signal →
 avatar-status.colloquial → ops「改写守卫」行（`ov2_av_guard*`）。
③ **前端超时 ≠ 发送失败**：send-voice 各阶段/终局落 `src/inbox/voice_send_tracker`
 （终局后迟到的 stage 更新一律忽略），前端超时走 `GET /api/unified-inbox/
 send-voice-status` 对账（sent/failed/unknown 三态；unknown＝保守提示+刷新会话，
 绝不诱导重发——幂等键拦不住人工重试的新键）；等待期渲染秒表+真实阶段；合成前后
 各挂一次 `record_audio` 会话状态（orch.send_chat_action，best-effort）；成功日志带
 `stage_ms=synth/conv/send`。
④ **音色偏好带身份快照**：localStorage 会话音色偏好为 `{v, ident}`（旧裸字符串
 兼容），会话换绑人设 → 快照失配 → 旧选择自动作废（「换了身份还粘着旧音色」的
 源头）；语音人设≠会话身份时 `voice-eff-risk` 行出「换人」警示（`__system__` 系统
 通用音色是刻意中性选择，不警）。
⑤ 发布纪律照旧：模板/i18n 热更新直上生产，每批 bump ui-build.txt；`.py` 改动攒批
 重启（本主线代码 2026-08-10 23:56 / 08-11 00:28 两批已装载 zhiliao）。

**真人感文本层 spoken_style 主线**（2026-08-11 收编 AvatarHub 交付包 PR#21/22；包本体
`platform/spoken_style/`（仓根），桥接 `src/ai/spoken_style_bridge.py`；L1-L3 已在 zhiliao
灰度（overlay `ai.spoken_style: {enabled, level: 2, zh_only: true}`），L4 刻意未开）：
```bash
python tests/test_spoken_style_bridge.py          # 桥接契约 10 例，零依赖可直跑
python ../../platform/spoken_style/smoke_test.py  # 包本体冒烟 26 项（异常先跑它分锅：包坏 vs 接入姿势不对）
```
预期：全绿。关键不变量（改 spoken_style_bridge / speech_prints.json / ai.spoken_style 前先读）：
① **分工边界**：包本体（L1-L4 模板/事实锁判据/`colloquial_rewrite.py`/`emo_tag.py`）＝
 AvatarHub 线，后两个文件是 avatarhub 仓**字节拷贝件勿本地改**（要改提示词走 avatarhub
 线同步）；指纹的角色化内容/档位缺省/灰度决策＝本线。桥接挂 ai_client 三处（L1 稳定
 system 段 / L2 轮变尾注 / L3 出口清洁）+ L4 改写出口，包缺席/加载失败/开关关＝全链
 no-op 绝不伤主链；**不注入包内人设卡**（chengjie 自有 persona 体系，只取正交层）。
② **指纹键契约**：`platform/spoken_style/data/speech_prints.json` 键＝人设**口称名**
 （`persona_manager.resolve_spoken_name` 输出**逐字一致**，含全角括号如
 `Marcus Wei（韦明远）`）；桥接按会话人设动态分流（`context._resolved_persona_name`
 → role 透传），配置静态 `role` 只是回落。**`profiles_runtime.yaml` 新增常驻人设必须
 同步加指纹条目**（漏了＝该人设只剩通用口语层没有指纹）；话少/播报型人设必须自带
 `guide` 覆盖默认碎句风格（反例样板＝包内「秦震」条目）。未绑定人设的会话走全局
 `ai_name`（顾嘉）条目。
③ **zh_only 必须开**（默认开）：L2 模板是中文口语指令，非中文主体消息（han <40% 或
 <2 字）不注入并计 `l2_skip_lang`——外语污染的直接读数；灰度验收＝外语会话出站不得
 出现「嘛/呢/啦」式中文语气尾。
④ **L4（rewrite）默认关**，开启前置＝L1-L3 灰度两天读数干净 + 与 `voice_colloquial_llm`
 **二选一**（后者改 TTS 前语音稿、L4 改文本主回复出口，叠开＝冗余+双份改写延迟；本机
 语音链正在用前者——见上方坐席语音主线）。开法＝overlay `ai.spoken_style.rewrite: true`
 （后端缺省 .173 qwen14b，`rewrite_llm`/`rewrite_model` 可换任意 OpenAI 兼容端点）；
 开后盯 INFO「spoken_style L4 汇总」（每 20 次尝试一行），直通率 >30%＝事实锁大量拒绝
 或后端超时，把读数反馈 AvatarHub 线调包，**别自己改提示词**。无指纹的 role 不改写。
⑤ **观测**：`spoken_style_bridge.stats()`（`l1_inject`/`l2_inject`/`l2_skip_lang`/
 `l3_changed`/`l4_attempt|applied|passthrough`）→ `/api/workspace/metrics.spoken_style`
 （drafts_routes 接线）；灰度复盘 CLI `python tools/spoken_style_obs.py`（只读：外语
 污染扫描 + 中文会话出站长度/语气词分布按灰度分界对比，多实例数据根自动发现）。

**收件箱筛选区去噪主线**（2026-08-11 P0-P4，修实录「红框里按钮太多、还显示不全，
不知道点哪」——405px 桌面壳下筛选区堆到 7 行占掉 196px 首屏，首屏只剩 6-7 条会话）：
```bash
python -m pytest tests/test_inbox_filter_usage_report.py -q --tb=line
python tools/verify_inbox_density.py        # 17/17，含 390x844 窄屏档
python tools/inbox_filter_usage_report.py   # 只读裁决（2026-08-25 起样本足）
```
预期：全绿。改 `unified_inbox.html` 筛选区 / `unified-inbox.css` chip 段前先读这九条：
① **「需人工」chip 与 rail 徽标同口径**：前端 `_STALE_ATTN_SEC = 72*3600` 必须与服务端
 `_ATTN_LOOKBACK_SEC` 同值——crit SLA 半边过 72h 窗，needs-human **标签**半边刻意
 不设窗（标签是人显式打的，不该自动过期）。**任一常量改动必须同改另一处**，否则
 rail 说 7 条、chip 说 33 条，坐席不知道信谁（上线前实况就是 33 vs 7）。crit 超 72h
 渲染中性灰 `.sla-chip.stale`，红色只留给真紧急——全红＝没有重点。
② **主筛选行单行横滚**：`.filter-tabs-primary.p0r` 必须 `flex-wrap:nowrap`（退回 wrap
 就是 7 行堆叠的老病）；左右溢出暗示靠 `.has-ovf-l/-r` 渐隐，`_syncFtabOverflow`
 必须两端都判（只判右侧时左边滚出去的 chip 毫无痕迹）。
③ **面板＝二级筛选唯一入口**（状态/目标议程/标签/排序四组）：即点即生效，**只有外点
 才关**——别再在 `setFilter` 里调 `_closeFtabMore`（连选多个条件时面板每次自关＝退回
 旧体验）；目标议程/标签/排序的 **DOM id 必须保留**，计数回填与 `_syncPanelUi` 靠它们。
 主行滚出视野的条件（如「我的」）必须在面板里有镜像入口 + 计入 `_FTAB_MORE`（否则
 筛选生效而入口不可见＝坐席以为坏了）。
④ **chip 规格单源**：六家族（ftab / goal-af / fp-chip / tag-strip / sla / badge）共用一份
 度量与 active 处理；`attn` 危险色靠属性选择器特异度存活。别再造第七套尺寸。
⑤ **窄屏抽屉 `bottom:64px`**：让开 56px 移动平台条 + 8px 间隙——8px 曾把「清空/完成」
 footer 埋在平台条下（静态门禁看不见，截图 QA 才抓到）；标签库下拉同为底部抽屉
 （一个抽屉一个顶部锚定＝同一手势两种去处）。
⑥ **内联 onclick 调的函数必须挂 window**：本区一天内两次实锤（`_uiBeacon` /
 `_closeFtabMore` 定义在 IIFE 内 → 点击抛 ReferenceError）。模板热更新直上生产，
 写完立刻补 `window.x = x`，别等哑按钮门禁。
⑦ **`resetAllFilters` 必须清 `_goalAgendaFilter`**：否则「清空筛选」后可见条件全清、
 目标议程还在暗处滤（幽灵筛选，坐席只会觉得列表数据丢了）。
⑧ **埋点前缀 `iflt_`**：新增筛选入口一律带 beacon，否则该入口**无法参与裁决**（标签
 strip / 原生排序 / 存视图三处就是事后补的）；`inbox_filter_usage_report.IFLT_EPOCH_DAY`
 是观察窗起点——**零点击本身是证据**，所以不能拿「数据首见日」当起点；改分桶要同步
 更新该常量。裁决口径与样本闸门全在该 CLI，别在看板里另算一套。
⑨ 发布纪律：css `?v=` + `ui-build.txt` **双戳**；密度 ratchet 天花板**只降不升**
 （当前 header 134 / 窄屏 149 / 单行 ftab 46 / 扁平会话行 74）。窄屏会话行 flatten
 后首屏 6-7 → 9 条，这是本主线的净收益，别让新徽标把它吃回去。

**A 线「被吞回复」补答/回滚主线**（2026-08-09，修 198↔104 实录「回复等了 11 分钟」三连）：
```bash
python -m pytest tests/test_reply_swallow_fix.py tests/test_message_dedup.py \
 tests/test_interject_absorb.py tests/test_telegram_outgoing_mirror.py -q --tb=line
```
预期：全绿。事故机制＝冷却/interject 静默吞回复且无补救调度 → 被吞消息只能等去重
TTL（600s）被轮询兜底碰巧重拾（实测表现「隔 10-11 分钟才回」）。三层不变量（改
telegram_client 回复管线 / skill_manager 冷却记账前先读）：
① 冷却拦截必须发跳过信号（`_note_reply_skip`/`consume_reply_skip`，取走即删）——
 上层才能区分「刻意不回」vs「被冷却吃了」；`_cooldown_remaining` 报**全部失败桶
 的最大剩余**（重试只有一次机会，只看首桶会撞上更长的 per_content 桶）；
 `_check_cooldown` 布尔壳签名被 test_group_context_split 钉住，勿动。
② client 两处冷却吞没点（回复逻辑闸 / skill 冷却信号）→ `_defer_swallowed_inbound`
 把该 mid 的去重寿命缩短到冷却结束后不久（`MessageDedup.reschedule` **只提前不
 延后**），轮询兜底按时重拾补答；水位闸保证至多重试一次绝不循环。开关
 `telegram.poll_fallback.swallow_reschedule`（默认开，随 poll_fallback 总闸）。
③ interject 丢弃已生成回复必须回滚 skill 侧冷却记账（生成前 `snapshot_reply_accounting`
 → 丢弃时 `rollback_reply_accounting`，只动时间戳 >= 快照时刻的本轮写入）——幻影
 回复不得占冷却位（否则补话本身也被吞、整个爆发窗全灭）、不得留在 last_reply/
 防复读环（防「AI 说过对方从没收到的话」穿帮）。`_update_after_reply` 三个记账
 写点被 `test_update_after_reply_stamp_contract_pinned` 钉住——挪写点先红。

**回复时延 SLO**（P1-8 2026-08-09，「被吞回复」事故的量化闭环）：
```bash
python -m pytest tests/test_reply_latency.py tests/test_ops_overview.py -q --tb=line
```
`src/ops/reply_latency.py`＝inbox 持久库口径（等待段=连续入站 burst → 下一条出站，
按**首条**入站计等待=客户视角最坏值；只算私聊、剔 bot/群/系统会话；出站不分
AI/人工——SLO 是客户体验口径）→ `/api/workspace/metrics.reply_latency`（300s TTL
缓存防 ops 轮询全扫消息表）+ Prom `ws_reply_latency_p50/p95_seconds`、
`ws_reply_unanswered_24h` + ops-overview「⏱️ 回复时延 SLO」卡（boss 组，
`ov2_s_rlat`/`ov2_rl_*` zh+en，零流量整卡隐藏）。「零回复」＝入站超宽限
（600s）无任何出站——与轮询兜底 TTL 同刻度，修复回归时此数先涨。

**回复额度守卫「设置页收口」主线**（P0 2026-08-12 已上线 zhiliao：peer_bot_guard 的
配置/救济/观测三合一进 `/reply-settings`「🛡️ 回复额度守卫」卡，此前配置只能改 YAML、
豁免只在收件箱触顶横幅；方案 `docs/REPLY_BUDGET_SETTINGS_PLAN.md`。P1（并行线交付：
spec 扩到全部 8 键→高级折叠可编辑、`budget_flags.near`≥80% 预警、watchdog 聚合告警
别名 `reply_budget` 带恢复通知、豁免撤销同端点 revoke）已随 2026-08-12 22:55 窗内
重启装载，只读互验 4/4（meta 8 键/缺省值/budget-today 行级 near）；回归命令追加
`tests/test_reply_budget_watchdog.py`）：
```bash
python -m pytest tests/test_reply_settings.py tests/test_peer_bot_guard_budget.py \
 tests/test_reply_budget_route.py -q --tb=line
```
预期：全绿。关键不变量（改 `peer_bot_guard.budget_flags` / 设置页守卫卡 /
budget-today 端点前先读）：
① **状态位语义单点**＝`peer_bot_guard.budget_flags`（纯函数）：`budget_state`（收件箱
 横幅/relief API）与 `GET /api/reply-settings/budget-today`（设置页「今日额度状态」表）
 都由它推导——两个消费面绝不各算一套（exhausted/hard_stopped 语义分叉＝坐席看到的
 与拦截行为对不上，比没有列表更糟）。
② **`daily_reply_budget=0`＝不限额**（evaluate 的 `budget>0` 闸 + `budget_flags.enabled`），
 **不是全拦**——门禁 `test_budget_flags_zero_budget_means_unlimited` 钉死。设置页 spec
 白名单刻意 clamp [5,500]：0 只许 YAML 配（防运营把 0 当全拦/把全拦当 0 误设；0 时
 触顶判定与收件箱横幅整体熄灭）。
③ **豁免（今日继续）单写入口**＝`POST /api/unified-inbox/reply-budget/relief`——收件箱
 横幅与设置页列表共用同一端点（列表端点逐行回传 platform/account_id/chat_key 三元组
 正是为直投它），勿造第二个豁免写入口。
④ **旧后端兼容＝feat 特性探测**：守卫字段挂 `feat:"guard"`（GET 快照 meta 缺键 → 整卡
 隐藏+收集/回填全跳过）——模板热更先于重启上线的中间态必须自洽；关总开关走
 confirm+审计（防轰炸防线整组失守，2026-08-03 实录 80 秒 78 轮空转）。
⑤ 台账批量口径＝`store.list_reply_budget_today`（LEFT JOIN conversations；used 跨日清零
 与 get_auto_reply_ledger 同口径；台账孤儿行如实回空三元组=前端豁免按钮不亮）。

**多开治理 / 防双发主线**（2026-07-29，修「同一坐席开两个窗口 → 客户收到两条一样的话」）：
```bash
python -m pytest tests/test_draft_resolve_concurrency.py tests/test_multiwin_p2.py \
 tests/test_live_drill_guards.py tests/test_settings_overlay_persistence.py \
 tests/test_workspace_home_nav_dedup.py tests/test_multiwin_review.py -q --tb=line
```
预期：全绿。三层防线（服务端兜底优先——前端协调只管同一浏览器，管不了第二台电脑）：
① **草稿处置原子闸门**：`update_draft_status` 带 `WHERE status IN ('pending','enriching')`，
 双窗口/人工×AutosendWorker 竞态只有一方成功，另一方拿 **409**（`already_resolved`）；
 worker 撞 409 计 `total_skipped_raced` **不计 error**（竞态非故障，不喂熔断器）。
 同批修掉两个休眠断链：`list_drafts(platform=X)` 此前排除 inbox 源草稿（坐席**看不见**待审草稿，
 实测积压 199h）；人工点「发送」此前**只标记不发送**（全库无人消费 approved 的 inbox 草稿）→
 现经 `DraftService.set_inbox_deliver_callback` → `AutosendWorker.deliver_human_approved`
 复用出站翻译/发图指令/桌面受控出站同一条投递链（bootstrap 仅在 `deliver=true` 时注入）。
② **发送幂等** `src/inbox/send_dedup.py`：前端每次提交带 `client_msg_id`，服务端按
 `(会话, id)` TTL 窗去重；**仅首见占坑**（重复命中记 duplicate 不新增 reserved），
 失败释放占位（同 id 显式重试仍可发）。覆盖 send / send-media / send-voice 三路由。
 **它只防「同一请求被重放」**（网络层重试等 → 同 id）；「用户连按两次」是两次独立提交、
 id 不同，服务端抓不住 → 客户端 `_sendInFlight` 闸门拦（发送按钮请求期已 disabled，但
 **Enter 键路径直接调 sendMsg 绕过按钮**，实测缺口；媒体路径另有 `_mediaSending`）。
 「两台设备各自输入同一句话分别发出」**刻意不防**——那是两次真实意图，改成内容级去重
 会误伤聊天里正常的重复短语（「好」「在吗」）。
③ **前端多窗口协调器** `workspace_base.html::__wsMultiWin`：坐席页单主控（localStorage
 主控位 + storage 事件；刻意不用 Web Locks——LAN http 下无安全上下文不可用），第二窗口出
 「在此使用/保持待机」，待机窗口停轮询断 SSE 全静音，主控崩溃 15s 内自动接管；
 全局**提示音领导权**顺带修掉「收件箱+看板两页同时响铃」。
 **保证边界（别误解成全局互斥）**：localStorage 只在**同一浏览器 profile 内**共享 →
 「桌面壳(Electron webview) + 浏览器标签页」「两台电脑」「Chrome + Edge」这些组合
 协调器互不感知，仍会各自响铃、各自可操作。跨设备真正兜住的是 ①（**草稿处置**的
 DB 级原子闸门，与进程/设备数无关）；② 的幂等键是**进程内**表（当前单实例成立，
 将来若多进程负载均衡需改共享存储）。③ 只负责同一浏览器内的体验（少噪音、少误操作）。
 跨进程互斥若将来真要做，数据地基已在：presence 心跳携窗口指纹 → `AgentCoordinator`
 按坐席聚合 `windows/standby_windows`（进程内 TTL 90s）。
④ **「返回工作台」去重**（P1-③ 2026-08-11，修老板实录「还会出现两个智聊坐席工作台」；
 遥测 mw.takeover_auto/reclaim 坐实同 profile 双坐席，源头＝子页/辅助窗的裸
 `location.href='/workspace'`（顶栏「聊天」/「← 返回工作台」/SSE toast/铃铛兜底）把 wsub
 辅助窗原地导航成第二个坐席）：`_win_unique.html::__wsGoHome`＝「别处有活跃坐席就交接
 （opener 直连 → BC 探活 250ms，cid 经 open-conv 送达 + toast `base.winuniq.switched`），
 没有才本窗原地导航」；消费面＝捕获相位委托拦截器（同标签 `<a href="/workspace">` 零接线
 自动获益）+ workspace_base 全部 SSE/铃铛兜底 + 四个子页 JS 跳转。三条硬不变量：
 ① 拦截器必须**捕获相位**且 _win_unique 保持在 <head>（先于 body 顶部 _loading_overlay
 注册）——否则「留在原页」也会点亮 15s 全屏加载遮罩（真浏览器门禁 8b 实锤）；真导航一律走
 `_navWithFeedback`（先 `__blLoadShow` 再跳，保住慢链路 L2 反馈）；② 普通 /workspace 入口
 与深链同走 BC 判重（`_bcHandoff` allowPlain；桌面壳普通入口不再每点弹一个新壳窗）；
 ③ 新增裸 `/workspace` JS 导航由静态 ratchet `tests/test_workspace_home_nav_dedup.py` 点名，
 行为由 `tools/verify_multiwin_ui.py` 场景 8（47 断言，场景 4 前先关坐席窗——普通入口探活
 后「首开」前提变了）钉住。PWA 应用窗维持 08-03 原地导航语义（app 窗自己是首选坐席容器）。
 **P2 增量**（同日）：① 耐心探活——探活 miss 先读坐席「在场心跳」（__wsMultiWin 主控位
 `aitr.mw.primary::/workspace`，5s 续租/15s 过期，键名跨文件契约有门禁）：心跳新鲜＝坐席
 在忙/被 Chrome 内存节省器冻结（冻结页答不了 BC ping，此前唯一漏判面），再等一轮
 （waitMs 800）；两轮静默且心跳仍新鲜 → 视同在场（frozen）提示切换，绝不双开。
 ② 交接 toast 中央化——`aitr:ws-handoff` 统一由 _win_unique 中央监听渲染
 （`_notifySwitched` 单一调用点有门禁），自带提示的页面置 `__aitrWsHandoffToastOwned`
 让行（cases）；修掉「入口页交接成功但静默＝用户以为没点上再点一次」的窗口翻倍诱因。
 ③ 验收周读 CLI `python tools/multiwin_review.py`（只读、多实例数据根、修复日切段判词：
 修复后 takeover_auto+reclaim 归零＝坐实；门禁 tests/test_multiwin_review.py）。
 ops goalRptLink 已接 winname 复用辅助窗。
**乐观锁**（防长编辑表单丢更新）：`persona_manager.profile_rev` 通用内容指纹 → 人设档案 /
 全局规则 / 意图关键词三处 `GET 回 rev → PUT 带 expected_rev → 不一致 409 → 前端确认后免键覆盖`。
 判据＝**整文档替换才需要**；字段级 patch 端点（`/api/settings/save`）刻意**不**加，
 否则「A 改 temperature、B 改 max_tokens」这类合法并发会被误判冲突（有反向门禁钉住）。
**观测**：`autosend-status` / `/api/workspace/metrics` 出 `total_human_delivered` /
 `total_skipped_raced` / `send_dedup`；Prom 5 个 gauge；ops「全自动媒体」卡两行 KPI。
**真机演练（运维显式跑，非 pytest）**：
```bash
python tools/live_multiwin_drill.py                 # 只读预检，不发消息
python tools/live_multiwin_drill.py --confirm        # 真发（目标=账号自己的 Saved Messages）
python tools/verify_multiwin_ui.py --shots out/      # 双标签页验协调器（只读，21 项）
```
`verify_multiwin_ui.py` 已挂进 `gate_sweep.ps1 -Full`（协调器是纯前端逻辑，静态门禁只能证
「函数挂了 window」、证不了「两标签页互斥成立」，而模板热更新直上生产）；缺 playwright
或实例不可达一律 **SKIP exit 0**，不污染回归信号。
**周批真发刻意不做**（每周往收藏消息累积杂物），改用**被动观测**：watchdog
`_check_human_deliver_chain`（webhook 别名 `human_deliver`）——同进程窗口内「有人真的通过过
草稿」却「投递计数恒 0 且零失败」＝一次都没尝试投递＝链断了。三个前提缺一不告警
（`deliver_enabled` / 人工通过数 ≥ min_approvals / 零投递且零失败），零成本且比周批更早。
门禁 `tests/test_human_deliver_watchdog.py`（15 例，重点覆盖「不该告警」的路径）。
⚠️ 统计人工通过数**必须查 `reply_drafts` 表而非 `draft_audit_log`**：`resolve_with_audit`
只在 L3/L4 或 autosend 时写审计，**L1/L2 的人工通过根本不落审计行**。

**被服务静态资产的落盘路径不变量**（`tests/test_static_asset_paths.py`，2026-07-29 实锤）：
凡「写了之后要经 `/static` 被访问」的目录**必须是 `Path(__file__)` 推的绝对路径**，绝不能用
CWD 相对路径——双实例部署的进程 CWD 是**实例数据根**，相对路径会把文件写进
`<数据根>/src/web/static/...`（web 服务从不挂载那里）→ URL 永久 404。实锤：
`account_self_profile._DEFAULT_AVATAR_DIR` 曾是相对路径，LINE 账号自身头像永久裂图，
且因指纹去重（`avatar_needs_refresh`）**不会重下、404 永久固化**；迁移前 CWD 恰好是代码根，
所以故障只在迁移后新登录的号上出现。门禁含全站扫描（禁 `"src/web/static/..."` 字面量）。

**相对路径并非一概有害**——落点该在哪取决于那东西是「数据」还是「代码」，用
`python tools/audit_relative_paths.py [--strict]` 分类审阅（A 被服务静态资产 / B 代码根资源
＝真缺陷；C 数据类 logs/config/*.db/tmp_* 落**实例数据根恰是想要的**＝无害）。
当前 A 类 0 处、C 类 32 处、B 类仅 1 处**刻意例外**：
`voice_prerender.DEFAULT_BASE_DIR = "assets/voices"`——写入方 CLI 显式按
`resolve_data_roots()` 逐根解析、读取方靠「引擎 CWD == 实例数据根」的启动契约与之同址
（实测 112 clip 全在数据根、引擎根 0）；两侧错开时失效是**软的**（回落现场合成
~7s vs ~200ms，不报错）**且已被 `prerender_coverage`/`prerender_miss` 观测覆盖**，
故刻意不重接管路。该例外由 `test_static_asset_paths.py` 里成对的两条门禁守住
（写入方必须保持显式 + 该处必须带「为什么刻意如此」的说明），别顺手「修」掉它。
`live_multiwin_drill` 四道护栏：默认只允许 `chat_key='me'`、真人会话须显式 `--allow-peer`、
无 `--confirm` 只预检、判定以 prometheus 增量为权威。护栏与判定算式有纯函数门禁
（`test_live_drill_guards.py`，含首次实施踩过的两个断言坑：reserved 增量算错 / 判定未按
本轮 TAG 限定导致误判双发）。真发落**生产账号的收藏消息**是刻意选择——隔离 dev 实例禁了
telegram 就发不出去（send 返 503），真发必须有真账号。
⚠️ **测试绝不能写仓库 `config/`**（该类事故已三次咬到生产，conftest 现有四层 autouse 隔离）：
`_isolated_account_registry`（假账号写进注册表 → 编排器当真账号无限重启 + 告警刷屏）、
`_isolated_global_rules`（路由测试把 13 条回复硬约束清成 `[]`，含「不要自称AI」这类安全项）、
`_isolated_audit_stores`（`autoreply_audit` / `ops_events` / `vision_metrics` 三个台账被掺测试
数据——运维靠 ops_events 判断「这号这周被风控几次」，掺假比没有更糟）、以及 import 期把
**`AITR_DATA_DIR` 指向进程级 tmp**（只剥不设会让所有按该约定定位数据的模块回落
`Path.cwd()/config`＝引擎根仓库目录；一次设定即隔离 persona_usage / telemetry /
desktop_selectors / licensing.data_paths / instance_restart_status 整个家族）+
`REUNION_PROMPTS_PATH` 指向 tmp（拷仓库真值过去，读到的内容与生产一致、只有写落 tmp）。
**新增测试若要写配置，一律重定向到 `tmp_path`，别改仓库文件、别加豁免。**

**怎么审计「有没有测试在写生产文件」**（比写通用守卫可靠）：跑一轮全量，然后按**回归时间窗**
比对 `config/` 的文件 mtime/size——落在窗口内的就是违规清单，精确且零假阳性。
⚠️ 别做「每个测试前后快照 config/ 目录、变了就点名」的 autouse 守卫：实测 243 个假阳性——
`-n auto` 并行下 worker A 的窗口会把 worker B 的写入算到 A 头上，`*.db` 被连接即改 mtime，
且共享工作树上**其他 agent 线**正在编辑 config 文件。精度 > 覆盖：走「按写入口精确重定向」。
`INTENT_TAGS_PATH` 亦已在 conftest 指向 tmp（拷仓库真值）：意图词表后台有整套写栈+备份轮转，
当前无测试真调那些端点（`test_admin_route_inventory` 只静态列 URL），这是**防以后**——
谁加一个真打 `/api/rpa/intent-tags/write` 的路由测试，生产词表就会被覆盖 + 备份轮转挤走真值。

**global_rules 落盘＝overlay 模式**（P7-1，2026-07-29；`test_global_rules_overlay.py` 9 例）：
旧实现读写同一个 `<repo>/config/global_rules.yaml`——那是**共享只读代码根**且被 git 跟踪，
于是运营在 UI 改全局规则就把仓库改脏（可能撞 `restart_instance.ps1` 脏树闸门）、打包态那是
只读安装目录根本存不下，也正是上面那起事故的土壤。现在：
**读**＝可写数据区（`AITR_CONFIG_PATH` 父 → `AITR_DATA_DIR/config` → 仓内，顺序复用唯一事实源
`licensing.data_paths.config_dir()`）那份存在就用它，否则回落仓库那份＝**出厂默认**；
**写**＝只写可写数据区，首次保存把出厂内容迁过去并进 `.bak.1`（运营点「恢复槽位1」＝回出厂）。
安全底线：读永远有回落，最坏「读到出厂默认」，**绝不读成空**（空＝所有人设丢硬约束）。
`GET /api/persona/global-rules` 多回 `source`（读/写落点 + `is_instance_override`），
把「出厂默认 vs 实例覆盖」的分叉对运维显式可见。
⚠️ 两个坑已踩过并有回归钉住：① `_global_rules_path` 现在**只表示显式覆写**，自动解析每次重算——
旧实现把解析结果缓存进该字段，首存后读路径仍钉在仓库那份 → 「运营改了却读不到」；
② 本机 `D:\boundless` 是指向 `D:\workspace\boundless` 的**目录联接**，出厂路径用 `resolve()`
而 `data_paths` 用 `abspath()` → 同一物理文件被算成两个路径 → 首存种子拷贝对同一文件调
`shutil.copy2` 抛 `SameFileError` → **保存静默失败**。两边统一 resolve + `os.path.samefile` 兜底。

**防双发防线周批**（P7-3）：`scripts\multiwin_drill_weekly.ps1` 把真发演练接成周期任务
（**刻意不进 `gate_sweep -Full`**——它真发消息）。实例不在线＝SKIP exit 0（不把「服务当时没起」
变成红告警），演练断言失败＝非零退出便于外部告警；UTF-8 日志落 `logs/multiwin_drill/` 保留 14 份。
`-DryRun` 只跑只读预检可验接线。注册（未自动建，需人工决定）：
```
schtasks /Create /TN MultiwinDrillWeekly /SC WEEKLY /D SAT /ST 07:10 /F ^
  /TR "powershell -ExecutionPolicy Bypass -File D:\boundless\engines\chengjie\scripts\multiwin_drill_weekly.ps1"
```

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

**图文一致性 P0 主线**（2026-07-27，修「发的图和说的话/时间/衣服对不上」四类实录事故）：
```bash
python -m pytest tests/test_persona_media_consistency.py tests/test_persona_media.py \
 tests/test_image_autosend.py tests/test_selfie_wiring.py tests/test_scene_state.py \
 tests/test_outbound_promise_guard.py tests/test_media_consistency_eval.py \
 tests/test_outfit_state.py tests/test_media_gap.py tests/test_media_restock.py \
 tests/test_image_gate.py -q --tb=line
```
预期：全绿。四层护栏（`companion.selfie.consistency` 默认开，`enabled:false` 一键回旧行为；
默认值集中在 `image_autosend.resolve_consistency_cfg`，A 线 skill_manager / B 线 autosend 同口径）：
① **场景硬匹配**——客户点名（`extract_requested_scene`）/承诺句点名（`outbound_promise_guard.
promised_scene`，兑现层升为硬要求）→ 注册相册 `pick_media(required_scene_class=)`（仅通用池受限，
运营触发词池最高优先）与文件系统相册 `_pick_from_album(required_scene=)`（挑不到 →
`album_scene_mismatch` 如实失败交诚实文字）**绝不顶包**；物体图生成失败绝不拿人像相册顶包
（`generate(allow_album_fallback=False)`，「要海景发车内自拍」收口）。② **时段软过滤**——
`tod:day|night`（DB tags / 相册目录 `_meta.json` sidecar，`album_file_meta` sidecar 优先于
文件名约定）与当前小时硬冲突剔除（22-6 不发白天照、6-17 不发夜景照、17-22 傍晚放行、无标注不判；
`tod_conflicts_with_hour` 纯函数）；剔空放行但 `extra["tod_softened"]=True`，配文层按旧照口径兜底。
③ **重发冷却 + 服装连续窗**——`persona_media_sends` 账本（A/B 线同一张表）推导：冷却窗（默认 24h）
内同图硬排除（`select_media(hard_exclude_ids=)` 任何回落层不放宽，全排除→拒发）；连续窗（默认
90min）内优先同系列未发图（`prefer_series`，防「30 秒换一身衣服」瞬移换装）；`pick_media(now=)`
与账本衰减同时钟。④ **配文诚实**——相册存货 freshness=old：LLM 配文指令显式禁「刚拍」
（`build_photo_caption_instruction(freshness=)`）、固定兜底走 `caption_album` 配置/双语变体池
（`_fixed_caption`，**绝不**回落「刚拍」口径的全局 `caption`；video 无旧照池只认配置）。
**跨人设隔离**：多人设分册布局下相册根目录不参与兜底（`_list_album`——根目录散图来源不明，
静默发出=「换脸换人」；显式共享走 `default_album_key` 语义不变；单人设平铺布局不受影响）。
**质疑观测**：`detect_media_complaint`（repeat/not_you/fake 保守词表）→ A/B 线
`_maybe_flag_media_complaint`（24h 内真发过媒体才采信）→ `record_media_complaint` →
autosend-status `complaints`/`last_complaint` + 回应纠偏 hint（别争辩/别再发同图）。
**元数据回填**：`scripts/persona_media_backfill_meta.py`（纯核心
`src/companion/persona_media_meta_backfill.py`）＝manifest/文件名/已有 sidecar 三源归并（人工值
优先）+ VLM（qwen3-vl，176/140 双活）昼夜**保守**分类（室内歧义=不打标）→ 写 `_meta.json` +
DB `tod:` 标签（幂等）；默认 dry-run 且 DB 走只读连接（对活体生产库零写事务），`--vlm --apply`
才写。门禁 `tests/test_persona_media_consistency.py`（32 例）。
**P1 增量**（同日，衣着状态机+生成链时段+缺口闭环；门禁 `tests/test_outfit_state.py` +
`tests/test_media_gap.py`）：① **今日衣着状态** `src/companion/outfit_state.py`——与
`resolve_current_scene`/`meal_state` 同哲学：crc32(persona#日期) 从衣橱池确定性取「今天穿什么」
（跨请求/跨 A、B 链同日恒定、明天自动换、零 LLM 零存储）；衣橱池优先级＝人设 `outfits` 字段 →
`consistency.outfit.pool` → **相册衣橱**（注册相册+FS 相册系列 slug 里认得出服装词的，
`series_outfit_phrase("white-dress")→"white dress"`，token 级词表防 rooftop→top 误命中；600s TTL，
尊重跨人设隔离）→ 内置中性便装池，首个非空层胜出；**连续性覆盖**＝媒体日志连续窗（90min）内刚发过
相册衣着系列 → 衣着跟随该系列（照片事实>每日默认，「再拍一张」不换装，`current_outfit(recent_series=)`
单一入口）。接线：A 线 Stage A/photo_directive + B 线 `stage_image_file` 生成 prompt 注入
`wearing <outfit>`（`build_selfie_prompt(outfit=)`）+ 聊天状态块同源（`scene_chat_note(outfit=)`
「你今天穿着…被问到与之一致」）；**媒体日志记 series**（`_record_media_sent(series=)`/B 线
`_notify_sent` 回调第三参）：相册图记条目系列、生成图记 `outfit_slug(衣着)`——生成↔相册跨链同一
连续性命名空间。开关 `consistency.outfit`（bool 或 `{enabled,pool}`，随 P0 总闸）。
② **生成链时段光线**：`ensure_time_of_day` 补齐 A/B 生成 prompt 的 `scene_hint`（深夜要图不再出
正午大太阳自拍；相册 `album_scene` 保持 raw 不受影响）。③ **投诉/需求分维观测**：
`record_media_complaint(kind=,persona_id=)` 分类型/人设计数 + `record_scene_request(scene,unmet=)`
（点名场景硬要求的 demand/unmet，A 线成功/发送失败/生成失败 + B 线全路径都记，认不出归 other）→
metrics `complaints_by_kind/complaints_by_persona/scene_demand/scene_unmet`。④ **相册缺口报告**
`src/companion/media_gap.py`：供给侧 `collect_scene_supply`（注册相册 DB enabled 图 + FS 相册
按 persona×场景类盘点，meta sidecar 优先，根目录平铺记 "" 共享池，300s TTL）× 需求侧（③ 的进程级
计数）→ 纯函数 `scene_gap_report`（未兑现↓→需求↓排序、共享池有货不算缺、`missing_personas`=有库存
但该场景 0 张的人设）→ `GET /api/admin/media-consistency`（?force=1 重扫）→ ops-overview
「🧷 图文一致性」卡（i18n `ov2_s_mconsist`/`ov2_mc_*` zh+en；零流量整卡隐藏）——「客户要什么场景、
缺什么」从翻日志变成看板照单补货。
**P2 增量**（同日，场景×衣着联动+出图后验+缺口自动补货；门禁 `tests/test_outfit_state.py`（P2 段）+
`tests/test_image_gate.py`（后验段）+ `tests/test_media_restock.py`）：① **场景×衣着联动 + 季节**
（`outfit_state`——衣着类别词表 `outfit_categories`（swim/sleep/sport/work/dressy/outer/light）×
场景规则表 `_SCENE_OUTFIT_RULES`（exclude=高置信违和：泳装进咖啡馆/办公室、睡衣进健身房、海边裹毛衣；
prefer=场景优先类：gym→运动装/beach→裙装轻装/office→通勤，衣橱有货才换）× 季节排除 `season_of`
（夏排重外套/冬排泳装/冬季**户外**再排短袖吊带——室内暖气吊带合理不动；`hemisphere: south` 倒扣）：
`adapt_outfit_to_scene` 纯函数＝换装（真实行为，确定性同日同场景恒定）或**放弃注入**（全池违和→
生图模型按场景自由穿，比硬注入冲突衣着诚实）；`current_outfit(scene=)` 单一入口内生效——A 线
Stage A/photo_directive/聊天状态块 + B 线 `stage_image_file` + 补货渲染六个消费口零改动自动获益，
连续窗衣着同样过闸（照片事实也不能泳装进办公室）。开关 `consistency.outfit.{season,hemisphere}`。
② **出图后验**（`image_gate`——`build_gate_prompt(scene_check/tod_check)` 在**同一次** VLM 体检
JSON 里顺带取 `scene`（14 类词表 `_GATE_SCENE_CLASSES` + 等价组 `scenes_equivalent`：home/bedroom/
kitchen 互认、cafe/restaurant 互认防误拒）与 `time_of_day`（day|night|unclear），零额外 GPU 往返；
`gate_verdict(expect_scene_class=,expect_tod=)` 只拒「VLM 明确判了不同」——`scene_mismatch`（点名
海边生成出健身房，prompt 层管不住的最后防线）/`tod_mismatch`（深夜要图出正午烈日照；`expected_tod`
仅无争议区间 8-16=day/20-4=night 才校验，晨昏两可不判），other/unclear/词表外一律放行不误伤；
不合格走既有换种子重试。接线 `generate_with_gate(expect_scene=,expect_hour=)`（A 线 Stage A/
photo_directive + B 线 + 补货渲染），开关 `vision_gate.{scene_check,tod_check}` 默认开。
③ **缺口自动补货**（`src/companion/media_restock.py` + CLI `scripts/album_restock.py`，与语音侧
auto_stock→夜间渲染同哲学；配置 `consistency.auto_restock` **默认关**）：watchdog
`_check_media_restock`（每小时 + 每日预算 max_per_day + pending 去重）把「P1 缺口报告里 unmet ≥
min_unmet 且该人设该场景零备货」（`qualify_restock_targets`；单人设布局补根册）写**计划文件**
`config/album_restock_plan.json`；渲染在**独立进程** CLI（建议夜间低峰）逐项真出图——衣着走 ①
场景联动（健身房补运动装图）、体检走 ② 全套含场景后验（补进相册的图保证真是该场景）、随机种子
（备货要多样性，stable_seed 会全长一样）、`allow_album_fallback=False`（补相册不许从相册自我复制）
→ 策展命名 `<场景>_<系列>_<nn>.jpg` 落 `album_dir/<persona>/` + 登记 `_meta.json`（scene/tod/
series）→ 供给/衣橱缓存失效——「看缺口→补图→下次有货」零人工。逐项落盘（中途崩溃已完成项不重跑）；
0 张也标 done 防死循环（重试由运营改回 pending）。观测：ops 卡 restock 行（`ov2_mc_restock*`）。
**P3 增量**（2026-08-08，配文诚实化——实录：自拍被配成「给你瞅瞅我卧室的样子」
「之前拍的存货，家里随便吃吃嘛」，画面里只有人脸。前面四层护栏管的是**挑哪张图**，
配文这条链一直是「LLM 看不到图、只顺着对话编」，所以图对了文照样穿帮）：
```bash
python -m pytest tests/test_caption_truthfulness.py -q --tb=line
```
① **P0 配文硬约束**（`build_photo_caption_instruction`，默认生效无开关）：人像类配文
 指令钉死「画面里只有你，没有食物/房间/物品」并显式禁「这是我做的 XX / 家里的存货」；
 相册分支此前**只传 freshness 不传 scene**（断链点）——现在把条目真实 `scene:*` 标签
 与 `wanted_subject` 一起喂进去，LLM 不必再猜。
② **P1 无货不发**（`outbound_promise_guard.wanted_media_subject`）：把「对方这次想看的
 **非人像主体**」从客户原话（「燕窝粥的照片」「给我看看你卧室」）与 **AI 自己的 offer**
 （「我煮了燕窝粥，要不要看看」→ 客户「好呀」）里抽出来——**offer 主体此前在
 offer-accept 桥上全程丢失，只剩一个 'image'**，这就是拿随机自拍顶包的根因。抽到主体后
 `pick_registered_media(deny_generic=True)` 关掉通用人像池（能归场景类的如「卧室」改走
 场景硬匹配）；相册后端无货 → **不发图**交诚实文字（`wanted_subject_no_stock`），真出图
 后端 → 改按物体图出该主体走 `image_gate` 后验。抽取宁缺勿滥（功能字/量词/人像词一律
 弃权，误抽会误拦正常自拍）。
③ **P2 出站前 VLM 图文核对**（`src/ai/caption_image_guard.py`，`companion.selfie.
 caption_guard` example 默认关 / zhiliao 已开）：只核 **LLM 现写的**配文（运营配文与固定
 池不声称画面内容），VLM 明确判「配文声称的东西画面里没有」才换诚实兜底配文，
 parse 失败/超时/无视觉后端一律放行原配文——**绝不阻塞发图**。观测
 `metrics_snapshot().caption_guard` + fallback 原因 `caption_guard_rewrite`。

**主动关怀（proactive_care）主线**（2026-08-01 当日 P0-P5 闭环，两条 agent 线接力；详版 DEVLOG §94）：
```bash
python -m pytest tests/test_care_routes.py tests/test_care_schedule.py \
 tests/test_care_dispatcher.py tests/test_care_commitment.py \
 tests/test_care_engine_hot_gate.py tests/test_care_extract_llm.py \
 tests/test_care_shadow_scan.py tests/test_care_shadow_report.py \
 tests/test_care_p2.py tests/test_care_budget.py \
 tests/test_overlay_comment_preserve.py -q --tb=line
```
预期全绿。关键不变量（改 care 前先读）：
- **常备接线 + 配置热闸**：捕获回调/派发循环/LLM 影子扫描无条件启动、内部按实时配置
 自闸——开关经 overlay 热重载 ~30s 生效**免重启**；`/api/care/engine` 三档
 `enable_dry→go_live`（强制先灰度）`→pause`；`/api/care/health` 四灯=唯一状态入口。
- **预览=派发同源**：`build_care_prompt` 单一入口，`/preview` 与真发一句 prompt 口径；
 dry_run 只拟稿不发送、样本进 `/api/care/dry-run-samples` 审核（👎 进全局黑名单）。
- **每联系人主动预算**（`contact_budget` 默认开，只拦真发）：**outreach_log 即共享账本**
 （proactive_topic 真发本就落账、care 真发经 sent_hook 落 `batch_id=care:*`），
 min_gap 4h + 本地日 2 次含本次；dry_run 不拦、危机关怀豁免、fail-open。
 反向让路（topic/ritual/milestone → pending care）由既有 `has_pending_care` 承担。
- **LLM 抽取双闸**（`llm_extract`）：`shadow`=影子对照只写 `logs/care_shadow/*.jsonl`
 （计数器随重启清零，JSONL 才是持久口径）；`enabled`=真实捕获**只收 llm_only**（正则
 优先）+ 同联系人**同事件日**日历去重（刻意不用语义嵌入——「一天一件事一条关怀」）。
 周审 `python -m scripts.care_shadow_report`；切主判据=llm_only 复核正确率 ≥80% 且
 样本 ≥20，且 health.shadow 快照含 `captured` 字段（=捕获代码已装载）。
- **overlay 写入必须保注释**：`set_overlay_flag`/`save_overlay_patch` 已走 ruamel
 round-trip（`set_yaml_key_preserving`/`merge_yaml_patch_preserving`，失败回落旧 dump）；
 **别再新增裸 yaml.dump 整写 overlay 的路径**（2026-08-01 实锤：一次 enable_dry 剃光
 config.local.yaml ~30 行运维注释，值无损但注释史没了）。
- 前端：页面词条全在 `i18n_packs/care_page.py`（`cs2_*`）；浏览器只读门禁
 `tools/verify_care_ui.py`（gate_sweep -Full 已挂，首跑即抓到 onboard-modal 拦点击）；
 ops 卡 `loadCare()` 读 health 零新后端。派发上下文用 `list_recent_messages`
 （**别改回 list_messages**——那是取最旧 N 条，「最近对话要点」喂成开场白的历史 bug）。

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

**AI 价值周报**（2026-08-06，「AI 本周替你干了什么」总账单一口径）：聚合纯函数
`src/ops/value_report.py::build_weekly_value`（reply_drafts / outreach_log / messages
**持久库 7 天窗**，重启不清零；触达回复判定与 `outreach_response_stats` 同款、剔 bot；
by_batch 分布与主计数同一 bot 剔除 SQL——首跑曾 159 vs 157 口径分裂）。消费面四路同源：
`GET /api/report/weekly` 的 `value` 段（F4 周报路由）、`_weekly_report_loop` 推送摘要
（`text_lines`）、ops-overview「🧾 AI 价值周报」卡（boss 组，`loadValueWeekly`，零流量
整卡隐藏）、**watchdog `ops_report` 事件 `value_lines`**（formatter 上限 6 行）。
⚠ 周报推送是**双栈**：F4 legacy 循环只走 `config.yaml::webhook` 旧栈（默认关）；
运营按推荐路径（告警渠道面板 → notify_webhooks.json）接通后收到的是 watchdog 的
`ops_report`（别名同名，`health_watchdog.weekly_report_enabled` 默认关）——价值行两条链
都在。门禁 `tests/test_value_report.py`（含三消费面接线断言）+ `test_ops_incidents.py`
（emit 携带 value_lines / formatter 渲染+上限）+ `test_ops_overview.py` 卡片三件套断言
（section/loader/注册表缺一＝静默缺陷）。

### i18n 施工约定（后台路由 CJK 收口 + 前端裸键）

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
4. 事后 `--verify-scope ROUTE_FILE` 复核（应空）；在 web_i18n.py 补齐 zh/en 两套键。

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
- ⛔ **本机已迁双实例部署（2026-07 起），禁止再用 `scripts\restart_main.ps1` / `start_main.ps1`**——
  它们的「杀所有 main.py + 引擎根老配置起单实例」语义会**同时杀死智聊/通译两个生产实例**并起一个
  抢同一 Telegram 账号的 18787 幽灵实例（2026-07-22 18:53 实锤事故：全站断连 + 看门狗被幽灵骗过）。
  两脚本已加双实例检测护栏（拒跑 exit 1），别绕过；
- **重启前先跑预检**（2026-08-06 沉淀）：`scripts\restart_preflight.ps1`——五步只读
 GO/NO-GO（10min 树静默+意向板 / 全量 dirty .py 语法 / app 装配测试 / -Advise /
 30min 手动间隔），任一红即 NO-GO（并按失败项给出下一步：`-Intent` / 修语法 / 搭便车验证）；
 全绿才给出重启命令。共享树重启装载**所有线**的落盘代码，预检是防「把 sibling 半存盘
 文件重启进生产」的机制化闸门；`-Advise` 本身也会指路先跑预检（三者互相引用，不靠记）；
- **正确重启（唯一入口）**：
 `powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance zhiliao`
  （通译改 `-Instance tongyi`）。脚本自带：只动一台、**机器级** 10min 冷却（与 watchdog 共用
  `D:\chengjie-instances\.ops\restart_cooldown\`）、仅模板/i18n 脏树时拒重启、清 exit 哨兵、等 `/login` 200、
  可选 ops 告警。不确定先 `-Advise`；应急 `-Force`；热文件仍要硬重启 `-AllowHotOnly`。
  ops 卡「实例重启冷却」=`GET /api/admin/instance-restart-status`。看门狗假活强制重启读同一冷却（冷却中跳过）；
  **不要**把 watchdog 当「改完代码必重启」；
- **攒批重启**：多项改动合一次重启，别每改一行重启一次（2026-07-12 / 2026-07-22 曾连环重启 →
  坐席端反复撞「加载超时」红屏——前端虽已有自动退避重连自愈，但窗口本身应尽量少出现）；
- **重启窗口**（2026-08-12 可靠性复盘 P0-3）：非紧急批次收敛到每日三窗
  **04:00 / 12:30 / 22:30（±45min）**——账本基线平均 8.4 次重启/天、88% 为开发批次，
  每次都是坐席 15-30s 断窗。`restart_preflight` 窗外会提示下一窗时间（仅提醒不拦截）；
  紧急修复 / watchdog 自愈不受窗口限制。
- 多 agent 并发**必须 worktree 隔离**（见 Git workflow），只有负责生产机的那条线才碰常驻服务。

### Git workflow

本 repo **2026-04-24 首次进 git**。现阶段：
- `main` 为主分支；baseline 见 `git log`（初次 import + CLAUDE.md + gitignore 强化）
- 后续 feature 走 `feat-*` 分支 + PR（参考 `mobile-auto0423` 的 squash merge 流程）
- **多 agent 并发用 `git worktree` 隔离**：普通分支只隔离提交历史，多个 agent 仍共用
  同一工作目录 → 文件互相串改、`index.lock` 互撞（本仓曾反复踩）。各 agent 各开
  `git worktree add -b feat-xxx ../telegram-mtproto-ai-xxx <base>`（独立工作目录 +
  独立 index、共用 .git refs），冲突只在 merge 时显式解决；收尾 `git worktree remove`。

### CWD 相对路径＝迁移后的静默失真（2026-07-29，双实例迁移遗留病）

生产进程的 **CWD 是实例数据根**（`D:\chengjie-instances\<inst>\data`），不是引擎根。
于是同一句 `open("config/config.yaml")` 在**服务进程**里恰好正确（落实例配置），在
**从引擎根跑的 CLI**（`python -m scripts.run_eval`、周批 `Set-Location` 落点）里却读到
迁移时刻遗留的旧副本。这类缺陷**不报错、不变红**，只是「用的不是在跑的那份」。

- **实锤 A（静态资产）**：自身头像 `_DEFAULT_AVATAR_DIR` 相对 → 写进
  `<数据根>/src/web/static/…`（**不被服务**）→ 永久 404，且 `avatar_needs_refresh`
  指纹去重不再重下，问题被永久藏住。改绝对路径 + 迁移错位文件。
- **实锤 B（评测配置）**：`src/eval/*` 四处 + `scripts/run_eval.py` 各自 CWD 相对读配置。
  与实例在跑的配置 **4/9 个关键键不一致**（`ollama_mt` 端点拓扑：引擎根 `base_urls=[140,176]`
  双活 vs 实例 `base_url=176` 单点；`per_lang_order` 引擎根有 hi 覆写 vs 实例已下线；
  嵌入端点 140 vs 176 优先）。A/B 实证：同命令仅数据根不同 → **38.0s vs 15.1s**（耗时差
  就是「先打 140」的证据），**两次都 10/10 PASS** ⇒ 光看报告永远发现不了。代价是归因
  失真：「弱语对该不该进 `per_lang_order`」正是读这些数字决策的。
  `run_eval` 的 `EVAL_LLM` 链更狠——**完全不合并 overlay**，而真 key 只在实例 overlay
  （实例 base 是 `YOUR_API_KEY` 占位），它读的是引擎根里的**另一把旧 key**。
- **收口**：`src/eval/eval_config.py::load_runtime_config`（复用 `scripts/_data_root`
  的唯一事实源：CLI 值 → `AITR_DATA_ROOT` → 自动发现活跃实例 → 引擎根；软回落不抛）。
  **注意别用** `licensing.data_paths.config_dir()`：它只认 `AITR_CONFIG_PATH`/`AITR_DATA_DIR`
  两个 env，CLI 场景两者都没设 → 仍回落仓内。
- **门禁**：`tests/test_eval_runtime_config.py`（禁 `src/eval` 再出现 CWD 相对读配置）
  + `tests/test_static_asset_paths.py`（已进 `gate_sweep`；内含 AST 全站扫描，复用
  `tools/audit_relative_paths` 的分类器当单一事实源，A/B 类零未登记条目）。
  自查：`python tools/audit_relative_paths.py --strict`。
- **分类口径**（工具 C 桶的前提）：`config/`、`logs/` 这类相对路径对**服务进程**无害
  （CWD 就是数据根），只对**从引擎根跑的 CLI** 有害——判风险要先问「谁在什么 CWD 下跑」。
- **优先级顺序本身就是那个模块的全部价值**（首版踩过，见 `load_runtime_config` docstring）：
  无条件走数据根解析会让 `monkeypatch.chdir(tmp)` 的单测**静默去读生产实例配置**，
  于是同一份测试「本机红、CI 绿」——与 global_rules 那次「测试写生产」同一类互串事故
  （`test_embedding_providers` 两条就是这么红的）。正确序＝显式实参 → **CWD 有 config 且
  CWD 不是引擎根**（保测试密闭 + 尊重「从某实例数据根直接跑」）→ 数据根契约（修「引擎根
  读旧副本」）→ CWD 兜底。第 2/3 条的唯一分界就是「CWD 是不是引擎根」。
- **影响面筛选别按文件名正则挑**（本轮唯一漏网根因）：我用
  `test_(autosend|draft|inbox|human|bazi|…)` 挑测试，漏掉了**直接测试我改过的模块**的
  `test_embedding_providers.py`，两条红要等 55 分钟的全量才暴露。改过 `src/X.py` 之后，
  按**导入关系**找测试（`rg -l "import .*X|from .*X import" tests/`）比按名字猜可靠。

**连带修好的评测保真度缺口**：修完配置解析后 `EVAL_LLM=1 --bazi-reading` 才真跑起来，
随即暴露「我明年运势如何？」两次稳定不合格（失地）。根因**不是模型退化**，是评测的
prompt 只喂四柱+大运，**缺聊天链才有的流年注入**（`extract_target_year` →
`liunian_detail` → `format_liunian_line`，那条防线本就是为「防 LLM 徒手编干支」建的）
——盘面里没有所问年份的干支可引用，LLM 只能泛泛而谈。既冤枉了产品，又让这条评测
**从没覆盖到那道防线**。补齐后 **9/9 PASS**（与 7/13 基线一致）。同步了 skill_manager
的「所问年==生年不注入」护栏与 `chart["day_master"][0]` 取法（别用 `day_gan`，键不存在）。

### 人工通过 ≠ AI 自动发（2026-07-29，错闸门）

`inbox.l2_autosend.deliver` 管的是「**AI 可否自己发**」；坐席点「通过」是**人的明示决定**。
把后者闸在前者上会让「AI 拟稿 + 人审后发」这个**最谨慎、最常被推荐的档位**里发送按钮
空转（`approveDraft` 只调 `/api/drafts/{id}/resolve`，**不走** send 端点；坐席以为发了、
客户什么也没收到，两端都看不出区别）。**决定性论据**：手动发送端点
`/api/unified-inbox/send` 本就不受 `deliver` 约束，用它闸人工通过自相矛盾。

- 缺陷面＝**出货默认档**：`config/config.example.yaml:2369` 就是
  `l2_autosend.enabled: true` + `deliver: false`（示例为安全起见默认不让 AI 自己发）
  ⇒ **worker 在跑、人工投递却没有发送能力**。换句话说「示例文件为安全而推荐的那个
  默认档，恰恰是坏的那个」，照抄示例部署的客户点通过全是空转。本机 zhiliao overlay
  置了 `deliver=true` 故一直正常，掩盖了这个默认档缺陷（通译实例已退役，不构成证据）。
- 修法：`AutosendWorker(human_send_callback=…)` 与自动链分开——deliver=false 时自动链
  `send_callback=None`（语义不变），人工链仍拿到真发回调（bootstrap 以
  `deliver_enabled=True` 另建一份）。注入改由 `inbox.auto_draft.human_deliver`
  控制（**默认开**；置 false 恢复「仅标记」旧语义）。
- 附带修真 bug：`_send_cb_kwargs` 原硬探 `self._send_callback`，deliver=false 时它是
  `None` → `inspect.signature(None)` 抛 TypeError → 缓存 `False` → 人工链**永久丢
  `original_text`**（语音分支据原文合成，丢了就用译文发声＝念错语言）。改为按**实际
  调用的回调**分别探测缓存。
- **接线状态可观测**（解掉「注入本身是静默的」这个盲区）：`DraftService.inbox_deliver_wired`
  → `/api/drafts/autosend-status.human_deliver_wired`（**worker=None 分支也给**——那正是
  最可能断的形态）。零流量即可判「链路在不在」，不必等真有坐席点过通过。重启后实测
  zhiliao=`True`。
- **看门狗判据同步升级为两档**（`_check_human_deliver_chain`）：接线状态**已知**时按它判
  （**不再看 `deliver_enabled`**——旧闸门会恰好在新能力所在的人审档闭嘴，这是本次改动
  带来的连带不一致，一起修了）；未接线＝确定性根因（告警带 `not_wired:true`，文案直接
  指路配置与 bootstrap 注入）；接线状态**未知**（读不到 draft_service，多见于测试/异常态）
  → 退回旧的保守闸门，信息不足宁可漏报不误报。运营显式 `human_deliver=false` 时全程静默。
- **`l2_autosend.enabled=false` 的人审档已闭环**：worker 整体不创建时，bootstrap 兜底建一个
  `deliver_only=True` 实例（**不 run() 自动循环**，只作人工投递载体）。刻意仍放
  `app.state.autosend_worker` 键——autosend-status / Prometheus gauge / 看门狗 / 报表都从
  那里读，塞同一键可让人工投递观测链**零改动全通**（比另起键+到处加回落更省更一致）；
  语义歧义由快照里的 `deliver_only` + `running/enabled` 显式消掉，另加正交字段
  `human_deliver_enabled`（人工链能力，与 `deliver_enabled` 互不代表）。
  人工发送不属 `ai_autosend` 授权范畴（手动发送端点本就不受其约束）。

**工作台预判徽标**（2026-07-30）：护栏只在**点下去之后**才说话，坐席撞 409 才知道
「这稿不能原样发」。故把判定收成 `DraftService._approve_block_reason` **单一入口**，
`/api/drafts` 每行带 `approve_blocked`（`""`/`age`/`replied`）→ 草稿卡显示**稿龄 chip**
（`_draftAgeText`：<1h 分钟 / <48h 小时 / 更久按天）+ 拦截 chip + 一句人话解释，
并把「发送」置灰、把「编辑」标成建议出路。**核心不变量＝预判必须与护栏行为完全一致**
（徽标另算一套 → 坐席看到「没标记」却被拦，比没徽标更糟），门禁用 8 组
(稿龄×是否回过) 交叉断言 `approve_block_reason == _stale_check 的 stale_reason`。
判定内部按稿龄短路（≤grace 不查会话、超龄直接定案），列表接口零 N+1 压力。
线上实测（重启后）：7 条待审里 6 条超龄被正确置灰、17.1h 那条正常可发。

**「已经回过了」是比年龄更准的判据**（同一护栏第二档）：坐席常走「采用文案 → 改写 →
手动发送」，而**发送路由不处置草稿行**（实测确认 `unified_inbox_send_routes.py` 里没有任何
resolve），那行于是永远 pending；投递接通后任何窗口点「通过」＝**再发一遍**（多开重复提交
的又一个入口）。故 `_replied_after` 走既有 `list_recent_messages`（DESC 取尾，不必改
他线在编辑的 store.py）判「草稿生成后是否已发出过回复」，命中且稿龄 >2h(grace) 即拦，
文案与「单纯过期」分开（`err.draft.stale_replied` vs `err.draft.too_stale`——一个是会重复、
一个是会脱节，坐席该做的事不同）。**刻意只认出站**：客户连发两条（纯入站推进）只说明
回复迟了，原样发仍合理，拦它只会白挡坐席；读消息异常一律放行。
2026-07-29 抽查生产 7 条待审：**0 例孤儿**（假设被证伪，没去修不存在的问题），但机制活着
且投递已接通，故按「已回过」直接拦。

**同轮必须配的陈旧护栏**（`inbox.auto_draft.stale_approve_hours`，默认 24h）：修好断链＝
把队列里的老稿子**变成实弹**。生产实测待审年龄 **5.0h ～ 213.1h（8.9 天）**、5/7 超 24h，
内容又极度依赖当下情境（「我刚到家，娃正在客厅拼乐高」「我现在就在 Seawall 这边」）——
隔周原样发出不是尴尬，是**当场穿帮**。故 `DraftService._stale_check` 在**处置之前**拦
（拦在 resolve 之后就又变成「标记了没发」）：只拦 `approve`（原样发）；`edit_send` 放行
（终稿是人写的）、`reject` 永不拦（要能清积压）、无 `created_ts` 不拦（宁可放过不误拦）、
未接线不拦（压根不会发）、`force_override` 是主管逃生门。阈值随投递回调同参注入
（`set_inbox_deliver_callback(stale_approve_hours=)`）——「能真发」与「需要护栏」是同一件事。
路由把**两种 409 分开**：`too_stale`（重新生成）vs `already_resolved`（刷新即可），坐席该做的
事完全不同，混成一句话等于误导。门禁 `tests/test_draft_stale_guard.py`（12 例，重点是
那些**不该误拦**的边界）。
- 门禁 `tests/test_human_deliver_independent_of_autosend.py`（deliver 关仍真发 / 无路径
  如实失败不静默成功 / 签名探测跟随实际回调且两回调缓存不串味 / 构造契约 / 静态接线
  不得再被 `if _deliver:` 包住 / 看门狗四档语义：已接线忽略 deliver、未接线带确定性根因、
  运营选择只标记则静默、接线未知退保守）。

### 草稿告警的 L1 盲区（2026-07-29，214 小时积压的真因）

生产待审队列实测 7 条、最老 **214h（8.9 天）**，其中 **6 条是 L1**。翻代码才发现三档
各有归宿、唯独 L1 掉在缝里：

| 档位 | 归宿 | 没人管会怎样 |
|---|---|---|
| L2（auto_ai + low） | AutosendWorker 自动发 | 照样发出去 |
| L3 / L4（medium/high） | `SLAWatcher` 逐条 `draft_sla_breach` | 会响 |
| **L1（review + low）** | **无自动发、无任何告警** | **无声烂掉** |

`SLAWatcher._check_sla_breach` 明确 `autopilot_level not in ("L3","L4") → continue`，
而 L1 恰恰是**唯一「必须人来处理」**的一档——告警洞正好开在最需要人的地方。
（那条 L3 也烂了 167h，说明还有第二层：SLA 只推工作台铃铛，`notify_webhooks.json`
默认 `enabled:false`，没人盯铃铛就等于没告警。）

修法＝`HealthWatchdog._check_draft_backlog`（**不动 SLA 那个较复杂的状态机**；
「没人在处理队列」是排班/注意力问题，属 ops 告警而非逐条草稿事件）：
- 聚合信号而非逐条（L1 是低风险日常稿，逐条＝噪音）：默认 ≥3 条超 24h 触发，4h 重提，
  队列**清空**才补恢复通知（部分消化不发，否则谎报「已处理完」）；
- 告警里单独点名 `sla_uncovered`＝其中多少条**不在** SLA 逐条覆盖内（本检查的存在理由）；
- 零误报前提：无 `created_ts` 的行不参与、取数异常静默、拿不到 draft_service 不猜；
- webhook 别名 `draft_backlog`，文案带分级分布 + 最老稿龄 + 处置建议（含
  `inbox.sla_watcher.auto_expire_hours` 这个长期缺人时的自动作废开关）。
配置 `health_watchdog.draft_backlog_remind.{enabled,min_age_hours,min_count,interval_min}`。
门禁 `tests/test_draft_backlog_watchdog.py` + `test_alert_delivery_e2e` 两个新 payload。

**精度：数「客户在等」而不是「草稿行还挂着」**。坐席走「采用文案→手动发送」时发送路由
不处置草稿行 → 那行一直 pending；把它算进「无人处理」就是**虚报**，运维开工作台一看
「其实已经回过了」就再也不信这个告警了。故 `DraftService.conversation_replied_after`
（护栏与巡检共用的公开入口）把两者分开：告警按「在等」触发，孤儿数走
`already_replied` 另报（清账即可，不是客户在等）；`by_level`/`oldest_hours` 同样只算
在等那批（否则孤儿会把最老稿龄撑大）。逐条要查一次会话消息 → `_BACKLOG_REPLY_PROBE_CAP`
（50）封顶，**预算外与判定异常一律算在等**（宁可多报不漏报，漏报＝客户真的没人回）。
旧 DraftService 无该方法时自动退化为纯年龄口径。

**线上实弹验证**（2026-07-29 19:52:55，搭他线重启的便车装载后 73 秒）：
`待审草稿积压：6 条超过 24h 无人处理（最老 215h，分级 {'L1': 5, 'L3': 1}；
其中 5 条不在 SLA 逐条告警覆盖内）`——与独立探针数据完全吻合，整条链端到端跑通。

### ⚠️ 告警出口当前为零（2026-07-29 实测，需运营决策）

启动日志实证：`WebhookNotifier 已启动（**0 个 webhook**）`。`config/notify_webhooks.json`
在**引擎根与实例数据根都不存在**，`config.yaml::notify.webhooks` 也是空、`webhook.enabled=False`。
即：本仓所有 EventBus 告警（SLA 越线、草稿积压、人工投递断链、host_alert、avatar_voice…）
**只进日志 + 工作台铃铛，没有任何外发通道**。那条 L3 草稿在 SLA 覆盖内却烂了 167h，
根因就是「报进了虚空」——**告警链的最后一公里从未接通**。

开通道需要 Telegram bot token + chat_id（属运营凭据，代码侧无法自造）。推荐路径：
后台「告警渠道」面板（`GET/POST /api/accounts/auto-reply/webhooks` + `.../webhooks/test`）
→ 写 `notify_webhooks.json` 覆盖层 + **热更 notifier 免重启**，不必碰 YAML。
建议先只订阅高价值别名（`draft_backlog` / `human_deliver` / `host_alert`）再逐步放开，
避免一次性把所有订阅事件推成告警风暴。

**自检面已闭环（2026-08-02）**：`GET /api/admin/alert-link-status`（文件真相×进程真相×
指纹分歧×引擎根诱饵检测，verdict＝no_channel/not_running/divergent/uncovered/healthy，
零密钥字段）→ ops-overview「🔔 告警链路」卡（未接通=红+接通 CTA；端点未装载=整卡隐藏）；
进程外文件口径用 `python tools/alert_link_selfcheck.py`。配套地基：`notify_webhooks_store`
缓存改按 **mtime 失效**（手改 JSON 下次 load 即见、删文件回 None——此前缓存永不失效，
自检会对着陈旧数据说一致）；`WebhookNotifier.status_snapshot()` 带非密钥字段 `config_fp`
（`alert_link_audit.config_fingerprint`，比对「磁盘 vs 进程装载」内容级分歧——手改文件
不经面板不会热更，一比即现形）。聚合逻辑单一事实源＝`alert_link_status.collect_alert_link_status`
（路由薄包装 + 健康灯 `health_watchdog.probe_alert_link` 同源消费）；**健康灯组件已接**
（`ops.alert_link_health` 默认开，0 通道/分歧/停摆＝黄灯软性绝不红灯，且 warn 不经
health_alert 外发——无循环告警）。门禁 `tests/test_alert_link_status_route.py` +
`tests/test_alert_link_health.py`。

### 多 agent 共享工作树并发协议（2026-07-28，当日三线并发实录教训）

worktree 隔离是理想态（见上），但实践中多条 agent 线常共用主工作树（生产实例共享
代码根 + 模板热更新的便利让隔离名存实亡）。当日实录踩过：同实例被两线各自重启触发
FLAP、内联按钮写了函数没挂 window 暴露块经热更新**直接上生产**变死按钮、
`unified_inbox.html` 三线先后编辑、ops 卡两线险些同时动工。共享树并发按以下四条，
每条都有当日实证：

1. **动手前探活**：`powershell -ExecutionPolicy Bypass -File scripts\agent_probe.ps1`
   ——列最近 30min 内变动的 dirty 文件（≤10min 标 ACTIVE=别人正在写）+ 高冲突热区
   标记 + 实例重启冷却状态。目标文件 ACTIVE（尤其热区）→ 避让或先协商，别开工。
   热区清单（历史互踩多发，工具内维护）：`unified_inbox.html`、`ops_overview.html`、
   `shared/copilot/**`、`skill_manager.py`、`src/web/i18n_packs/*.py`。
2. **热更新＝直接上生产**：模板 / i18n pack / 共享组件保存即生效（Jinja auto_reload
   + mtime 热加载），没有「未部署」缓冲。分步保存时**每一步都必须自洽**——当日实录：
   `onclick="_openGoalQuick()"` 的函数定义了、window 暴露块没挂，死按钮在生产工作台
   存活到广域门禁扫出为止。
3. **重启搭便车**：代码根共享 → 任何一次实例重启装载**所有线**已落盘的 .py。重启前
   看冷却（probe 第 2 段）：30min 内别人刚重启 → 你的改动大概率已被装载，用只读探针
   验证（打一发新路由 / readiness 看新字段）而不是再吃一次重启窗口（当日实录：11:57
   对方重启已装载本线 P15 代码，12:40 又重启一次即触发 FLAP 告警）。
4. **收口跑广域门禁，不只跑自己的**：一条命令
   `powershell -ExecutionPolicy Bypass -File scripts\gate_sweep.ps1`（清单在脚本内
   维护：前端接线全套 + i18n 契约 + 共享组件双树同步/主题 token + 路由契约；
   `-Full` 追加全量回归）。当日两例跨线 bug（哑按钮、暗色 token 缺口）都是甲改
   乙扫出来的——门禁互验是共享树上唯一可靠的「代码评审」。别人文件红了先报告，
   别在对方活跃编辑窗内默默替改。**红灯账本**（2026-08-12 加，「5 个既有红互相
   指认 other owners 挂了 24h+ 无人修」事故沉淀）：sweep 自动把每个失败门禁的
   首见时间记进 `D:\chengjie-instances\.ops\gate_reds.json` 并在输出里带 age，
   **>48h 亮 `UNCLAIMED` 点名**——看到点名就三选一：修掉 / 按台账语义登记留债 /
   意向板认领；绿了自动销账。**瞬态红自动复跑**（同日晚加）：sweep 与 sibling
   保存并发时会扫到半写文件（当日两例实锤：ui-build 新鲜度/时间炸弹门禁各红了
   恰好一轮）——第一轮有红时 sweep 自动把失败门禁**文件**串行复跑一次，第二轮
   为权威（自愈=verdict 转绿并注明 transient；仍红=真红），账本只记权威轮。
   夹具时间炸弹（硬日期跌出 now 窗自爆，duel 实锤）有专项门禁
   `tests/test_fixture_time_bombs.py`：trend 夹具日期一律锚 now。
5. **前端批次双戳**（2026-07-29 账号 rail 事故沉淀）：模板/CSS/i18n 热更新后，
   **开着的旧标签页仍跑旧 JS**。每批前端落地必须同时改两处——
   `src/web/static/workspace/ui-build.txt` 首行（陈旧页横幅轮询此文件）+ 相关
   CSS 的 `?v=` 缓存戳（如 `unified-inbox.css?v=`）。只改功能不 bump =
   「修好了坐席还在踩」。`-Full` 含三个收件箱真浏览器门禁：
   `tools/verify_account_rail_ui.py`（账号视角不变量）、`verify_inbox_density.py`
   （会话面板密度预算 + 「可见但空」扫描，2026-07-30）、`verify_inbox_identity.py`
   （人设身份真相：回复区身份条会话覆写/TTL 缓存/换绑即时校正 + 行徽章行级
   eff_persona，route mock 零生产写入，2026-07-30）。三者环境缺失一律 SKIP exit 0。
6. **意向板**（2026-08-05 沉淀：两条线同小时内为同一事故各建了几乎相同的验证工具，
   靠代码注释里的工具名才避免重复上线——mtime 探活只能看见「在哪打字」，看不见
   「在追什么」）：开工时 `scripts\agent_probe.ps1 -Intent "主题 (涉及文件/区域)"`
   登记（建议 ASCII），收工 `-Intent "同主题" -Done` 清除；探针 [3/4] 段展示各线
   24h 内的在册意向（过期自清）。登记落 `D:\chengjie-instances\.ops\agent_intents\`。
   动手前先看意向板：主题撞车 → 先合流（复用/增强对方产出）再动工。登记成功后提示
   指向 `restart_preflight`；预检 NO-GO / `-Advise` 也会回指意向板——形成闭环。
7. **index 使用协议**（2026-08-27 沉淀：同一天被卡两次）：git 暂存区是**整个工作树
   共用的一个槽**，不像 dirty 文件那样能各写各的。任一条线有文件 staged 期间，别的线
   要么干等、要么在 `git add -A` / `git commit -a` 时**把对方的文件顺手扫进自己的提交**
   （当天真发生过，事后要用 plumbing 才拆得开）。三条：
   - **暂存后几分钟内必须提交或 `git reset` 释放**。「先 stage 着，等下再说」＝在锁着
     全楼的门。忘了释放的多半是自己，不是别人。
   - **`stage_hunks` 发现外来暂存直接拒绝**（已实现，`--allow-foreign-staged` 是逃生门，
     用之前先确认那些文件真不是别人在途的）。
   - **大批收口安排独占时段**：几十个文件的整理别和别人的日常提交挤在一起。
   探针 `[1/4]` 段直接报 index 是空闲还是被占（含文件清单）——被卡时先看它，别猜。
   ⚠ 提交一律**显式列文件**：`git add <明确路径>`，永远不要 `-A` / `commit -a`。
8. **绝对禁止 `git reset --hard` / `git checkout .` / `git restore .`**（2026-08-29
   06:26 实锤，代价最大的一次）：这棵树上常年有 100+ 个 dirty 文件、属于**多条并行
   线**，工作树是共用的一个。那次 reset 把**所有线**未提交的改动一次抹平（reflog:
   `reset: moving to HEAD`）；untracked 新文件侥幸存活，已跟踪文件的修改全灭。
   实际损失：小智线丢掉 SSE 断流真凶修复（`admin.py` 退回伪造 `http.disconnect`
   的版本，而 `body_replay.py` 因为是新文件还在、只是**没有任何调用方**）、四层
   作答、拖拽缩放与主题桥、以及全部门禁改动；另一条线的 `assistant-teach.js`
   教学行结构与 `asb_h3_ask` 埋点**至今没人救回来**（3 条门禁持续红）。
   - **最阴险的地方是当时看不出来**：常驻进程内存里还是好代码，线上一切正常，
     故障要等到**下一次重启**才装载回滚后的磁盘并静默复发。所以它不是「当场
     报错」而是**定时炸弹**，谁重启谁背。
   - 只回滚**自己的**文件：`git checkout -- <明确路径>`（逐个列，别用 `.`）；
     要临时收起自己的改动用 `git stash push -- <明确路径>`。
   - **别指望 reflog 能救**：它只记 HEAD 移动，未提交的工作树内容压根不在 git 里，
     `reset --hard` 之后没有任何 git 手段能取回。
   - 推论（这次的真正教训）：**在这棵树上不要长时间留未提交的工作**。做完一批、
     门禁绿了就显式列文件提交；提交是这里唯一可靠的保险，不是流程洁癖。

### 崩溃恢复提示

- 本项目不在 git 之前的工作记录在 `DEPLOYMENT_STATUS.md` / `TODO_NEXT.md` / `docs/` 下多份 `*_PLAN.md` 与早期分析（历史文档，可能已过期，**以代码为准**）
- 已知含**虚构 model ID** `claude-4.6-oups-high` 的 deprecated docs（不要被这些占位误导）：`CURSOR_DEVELOPMENT_GUIDE.md`、`CURSOR_HANDOFF.md`、`docs/MONITORING_PLAN.md`、`docs/MONITORING_API_SPEC.md`、`docs/ORDER_REPLY_GENERATION_ANALYSIS.md`、`docs/LOG_ANALYSIS_OPTIMIZATIONS.md`——本 repo 实际 ai provider 见 `README.md` + `config/config.yaml::ai`
- `~/.claude/projects/C--telegram-mtproto-ai/memory/` 里 `MEMORY.md` 按项目分组，本项目条目见 "Project: telegram-mtproto-ai" 段
- 关键教训：`project_tasklist_drift.md` — 文档落后于代码，重入时以 `grep` 验证代码实况再信任任务列表

## 不在本 repo 范围（见 PROJECT_SCOPE.md）

Facebook add_friend / greeting / auto_reply / VLM Level 4 fallback 栈 → `github.com/victor2025PH/mobile-auto0423`
