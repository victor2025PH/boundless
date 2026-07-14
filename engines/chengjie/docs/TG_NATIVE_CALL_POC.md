# Telegram 原生语音通话 —— PoC 操作手册与传输层选型

> 目标：AI 全自动接听 Telegram **原生电话**做情感陪护（非 Mini App、非卖产品），
> 极致真人感。本文是 P0 阶段的传输层验证操作手册 + 立项闸门判定标准。
> 代码：纯函数核心 `src/voicecall/core.py`（门禁 `tests/test_voicecall_core.py`），
> PoC 验证台 `tools/tg_call_poc.py`。**默认全关**（`config.yaml::telegram_calls.enabled=false`）。

## 架构总览

```
Telegram 来电
  │  传输层（CallTransport 协议，二选一）
  │   ├─ A. py-tgcalls 3.0 + ntgcalls 3.0（进程内直连，绑现有 pyrogram client）→ NtgcallsTransport
  │   └─ B. tg2sip WebRTC 分支（独立 SIP 网关，call layer 全覆盖）→ 自建 SIP UA transport
  ▼
通话桥 src/voicecall.bridge.TelegramCallBridge
  │   决策 decide_incoming_call / 状态机 transition / 10ms 帧数学 / 拟人调度 / 安全监测
  │   出向：大脑 PCM → audio.resample 48k → core.split_pcm_frames 10ms → transport.send_frame
  │   进向：transport 帧 48k → audio.downmix+resample 16k → brain.push_audio（按 chat_id 路由）
  ▼
实时语音大脑（CallBrain 协议，二选一）
  ├─ s2s（P1 现实默认）：MiniCPM-o 4.5 全双工 → RealtimeS2SBrain（复用 realtime_voice，零新增显存）
  └─ cascade（未来态）：流式 ASR + generate_reply 全链 + CosyVoice3 流式（音色主权，需专用 GPU）
```

**代码落位（本轮 P1 地基已建，默认全关，全 import-safe 无重依赖）**：

| 文件 | 职责 | 门禁 |
|---|---|---|
| `src/voicecall/core.py` | 配置/决策/状态机/帧数学/拟人调度（纯函数） | `test_voicecall_core.py`（36） |
| `src/voicecall/audio.py` | PCM16 重采样/折叠/时长（纯数学+numpy 加速） | `test_voicecall_bridge.py` |
| `src/voicecall/safety.py` | 转写并行危机监测（复用 detect_crisis 单一事实源） | `test_voicecall_bridge.py` |
| `src/voicecall/bridge.py` | 编排器（决策→会话→双向中继+安全+拟人+观测→收尾），协议注入 | `test_voicecall_bridge.py`（29） |
| `src/voicecall/humanize.py` | 思考填充音 + 倾听反馈并行调度（事件驱动+tick，时钟可注入） | `test_voicecall_humanize.py` |
| `src/voicecall/call_stats.py` | 通话观测单例（接听率/拒接原因/时长/拟人/安全升级，dump+prom） | `test_voicecall_humanize.py` |
| `src/voicecall/adapters.py` | RealtimeS2SBrain（S2S 大脑）+ NtgcallsTransport（传输，惰性） | `test_voicecall_adapters.py`（9） |
| `src/voicecall/wrapup.py` | 收尾闭环：转写→接地事实落库→挂断后 follow-up（纯装配+注入 IO） | `test_voicecall_wrapup.py`（9） |
| `src/voicecall/health.py` | 主机健康探测（复用 realtime_voice 主机）+ 开闸前就绪度体检（纯函数） | `test_voicecall_health.py`（15） |
| `src/voicecall/call_usage_store.py` | 按账号通话用量（滚动 24h 次数/分钟，跨重启存活，喂预算闸） | `test_voicecall_usage_wiring.py` |
| `src/voicecall/wiring.py` | `assemble_call_context`：散落信号经注入型 lookup 组装成 CallContext | `test_voicecall_usage_wiring.py` |
| `src/voicecall/runtime.py` | `build_call_runtime` 单一装配入口 + on_incoming 路由 + dry_run_report | `test_voicecall_runtime.py`（7） |
| `tools/tg_call_poc.py` | 传输层三闸门验证台（双号自动互拨，测试号专用） | 语法+安全闸 |
| `tools/tg_call_dryrun.py` | 投产前接线完整性自检（零连接零通话） | 真机已跑通 |

**健康灯 + 升级式告警**（镜像 `_check_avatar_voice`）：`HealthWatchdog._check_native_call`
探 MiniCPM-o 主机（brain=s2s 复用 176:7860），持续不可用 ≥`after_min`(默认30) → 首提
EventBus `tg_call_alert`（webhook 订阅别名 `tg_call`），每 `interval_min`(默认240) 重提，
恢复补发；telegram_calls 未启用/非 s2s → 探针 None 天然静默。主机挂=来电全接不了、
「她会接电话」卖点静默失效，比看板黄灯更该主动轰人。配置 `health_watchdog.tg_call_remind`。

**开闸前就绪度体检**（`evaluate_call_readiness`，纯函数）：给定「配置+主机探测+参考音摘要+
传输就绪+auto_ai 会话数」产出 `{ready, blockers[], warnings[]}`——blocker=开了也不工作
（主机不可达/模型未载入/传输未验证/无 auto_ai 会话），warning=能工作但打折（无参考音降级内置
音色/参考音红灯/cascade 硬件嘴达不到实时）。对齐陪伴能力看板「看→校→开」范式。

**已接进看板**（route + ops 卡）：`GET /api/voice/call/readiness`（复用 probe_call_host +
参考音摘要 + auto_ai 会话计数）→ ops-overview「📞 原生来电」卡**分三态**：有来电=读数；
零来电但已开=显示 blocker/warning「差哪些」（不再整卡隐藏）；未开=隐藏。i18n `ov2_tc_*` zh+en。
路由已登记路由清单基线。

**传输层已验证闸**（`telegram_calls.transport_verified`，默认 false）：ntgcalls #44 进向音频 /
tg2sip 网关是运行时无法自证的最大未知——只有跑过 `tools/tg_call_poc.py` 三闸门确认能收发音频，
运营才手动置 true。就绪度体检据此判定：**未验证 → blocker**（哪怕主机在线也不算「就绪」），
从产品层堵死「看板绿灯了但真机根本收不到来电」的误判。

**账号级通话预算/健康闸**（`telegram_calls.budget`，`core.evaluate_call_budget` 纯函数）：
一通 AI 语音通话是**比发条消息强一档的 userbot 特征**，故给通话独立的、比 send_gate 更保守的
日预算——`daily_calls_cap`(默认20) / `daily_minutes_cap`(默认60) / `block_on_red`(account_health
红灯停接)。超额/红灯 → **拒接+补偿**（熟人只是今天聊太多/号要歇歇，绝不空响），不静默。
`ctx.{calls_today,minutes_today,account_light}` 由 wiring 从 call_stats + account_health 组装
（per-account-per-day 计数是 wiring 侧的持久化，可复用 account_sends.db 式存储）。

**收尾 follow-up 走 deferred 队列**（`wrapup.make_deferred_follow_up`）：挂断后 follow-up
**入 `companion_proactive` 的 deferred outbox**（而非自建发送）→ 天然继承 kill-switch / 安静时段 /
pacing / staleness 全套护栏，绝不把「关心」发成「骚扰」；延迟 3-7min 随机（真人挂了电话不秒回）。

**用量记账闭合预算环**（`call_usage_store.CallUsageStore` + wrapup 记账）：预算闸读
`calls_today/minutes_today`，数据由收尾时 `make_wrapup_hook(usage_record=...)` 写入
`CallUsageStore.record_call(account_key, duration_sec)`——**滚动 24h 窗口**（非自然日，防午夜
绕过）+ 跨重启存活（否则重启即归零=日预算形同虚设）。只要接通过就计入（哪怕 5 秒，它占用了
主机 + 是 userbot 信号）。风格/清理同 `SendCountStore`。

**上下文组装器**（`wiring.assemble_call_context`）：真机来电只带 `(account_id, chat_id)`，其余
决策信号（会话语言/档位/亲密度、通话用量、账号健康灯、kill-switch、记忆）散在各 store。本组装器
把这些**查询**收敛成注入型 lookup，拼出 `CallContext`：每个 lookup 缺失/异常 → 保守默认
（查无会话=陌生人→静默拒接；用量/健康 lookup 崩→按 0/green 保守），绝不因某信号源抖动拒接
合法来电或误判绿灯。wiring 侧传真实 store 方法，测试传 fake。**真机接线只剩「塞真实 store 实例」**。

**观测已接进看板**（与 realtime_voice/avatar_voice 同构）：`call_stats.dump()` →
`/api/workspace/metrics.tg_call`、`dump_prom()` → Prometheus（`tg_call_*`）、ops-overview
「📞 原生来电」卡（零来电=整卡隐藏，i18n `ov2_tc_*` zh+en 齐备）。

**收尾闭环铁律**（Phase8 幻觉教训的直接应用）：事实**只**从 `transcript.user`（用户亲口说的）
抽取，并用用户原话 `filter_grounded_facts` 接地——`transcript.assistant`（AI 的话）永不入记忆；
severe 危机通话**不 follow-up**（防二次刺激，安全交人工）但仍落库；极短/未接通话不落库不追消息。

**拟人层三件套（真人感的差异化来源，全部预渲染克隆声、零 GPU 零延迟）**：
- **opener**：接通即播「喂？/在呢~」——解 `realtime_voice.opener` 因 20s 一次性合成被迫关掉的老问题；
- **filler**：LLM 思考 >700ms 未出话 → 插「嗯…/让我想想」，掩蔽半级联/冷启首音延迟；
- **backchannel**：对方长段倾诉 → 插「嗯嗯/然后呢」（VAD「对方开口」信号经 `on_user_speech_start`
  按 chat_id 路由驱动；cascade 需要，S2S 原生自带可不注入）。
三者音频供给都是注入型 `PcmProvider`（async），**无 provider = 该能力静默关**（无预渲染资产时
正确降级，绝不发静音噪声）。并行 tick 循环随会话结束干净 cancel，异常不外泄。

安全并行监测是 **S2S 失去「出口前拦截」的补偿**：`transcript.user` → `assess_call_transcript`
（同一 `detect_crisis`）→ severe 注入安全指令 + 拉人告警 / elevated 温柔指令。

## P0 立项闸门（三个必须全绿）

| 闸门 | 判定 | PoC 证据 |
|---|---|---|
| ① 自动接听 | 能捕获 `INCOMING_CALL` 并 accept | 日志 `[poc] INCOMING_CALL` + `answered` |
| ② 出向音频 | 我方 PCM 送达对端（对端听得到） | 日志 `OUT sent frames` + 对端真机听到 440Hz 单音 |
| ③ 进向音频 | 收到对端 PCM 帧（**ntgcalls #44 顽疾**） | 日志 `IN recv frames` 持续增长 |

**任一不过 → 切换传输层备选或冻结项目**（诚实止损，不硬扛上游 bug）。

## 环境实测结论（2026-07-14，本机 Python 3.13 / Win）

已核实（`pip install` + API 内省，非猜测）：

- **wheel 齐备**：`ntgcalls==3.0.0b2` 有 **cp313 win_amd64 预编译 wheel**（24 个文件），
  `py-tgcalls==3.0.0.dev2` 是纯 Python wheel。两者 `pip install --only-binary` 一次成功，
  **无需本地编译**（消除了「cp313 wheel 缺失」这个原方案里的头号风险）。
- **重大利好——3.0 把私聊双向原始音频做成一等 API**（2.x 时代 PoC 需绕过 pybind11）：
  - 出向：`calls.send_frame(chat_id, Device.MICROPHONE, pcm, Frame.Info(...))`；
  - 进向：`calls.record(chat_id, MediaStream(ExternalMedia.AUDIO))` +
    `@on_update(filters.stream_frame(directions=Direction.INCOMING, devices=Device.MICROPHONE))`
    处理器收 `StreamFrames`；
  - 来电事件：`@on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))`；
  - 接听配置：`CallConfig(timeout=...)`。
  → 意味着 **闸门③ 有官方 API 路径**，不必再像旧 PoC 那样 hack ntgcalls 源码。
    但「API 存在」≠「音频真的流动」——#44 的静默是否在 3.0 重写中被修好，
    **只能用真机通话实测**（`tools/tg_call_poc.py` 的 `IN recv frames` 就是判据）。
- **帧节奏铁律**：ntgcalls `AudioSink.frameTime()` 实测 **10ms**（非 20ms 误解）。
  48kHz/10ms/mono/PCM16 → **960 字节/帧（480 采样）**。发 20ms 帧 → 对端 jitter
  underrun 卡顿/爆音。`core.frame_bytes()` / `FramePacer` 已按 10ms 固化，门禁钉死。
- 语音主机健康：176:7860 MiniCPM-o `model_loaded=true`（22.4GB 占用）、本机 7852
  CosyVoice `models_loaded=true`，**7852 已有 `/v1/tts/stream` 流式端点**（4 字节小端
  长度前缀 + PCM16 裸流）→ cascade「嘴」的流式基建现成，`core.iter_length_prefixed_pcm()`
  已对齐该协议。

## 传输层 A：py-tgcalls 3.0（进程内直连）—— 首选验证

**优点**：绑现有 pyrogram client（与消息 worker 多会话并存）、部署最轻、API 原生。
**风险**：#44 进向静默历史；Telegram call layer 漂移（3.0 用 tgcalls 8.0+9.0 回落）。

**双号自动互拨 PoC（无需人工拨号）**：`tg_call_poc.py` 支持 `TG_CALL_POC_ROLE`——一台
`answer`（待命接听，验证进向 #44），另一台 `call`（主动拨 `TG_CALL_POC_PEER` 指定的对端 +
送 440Hz 出向正弦，验证出向）。**两个空闲测试号各跑一个角色即全自动验证双向音频**：

```powershell
# 号 B（被叫，验进向 #44）——终端 1
$env:TG_API_ID="B_api_id"; $env:TG_API_HASH="B_api_hash"
$env:TG_CALL_POC_SESSION="B_session_string"
$env:TG_CALL_POC_CONFIRM="i-understand-test-only"; $env:TG_CALL_POC_ROLE="answer"
python -m tools.tg_call_poc

# 号 A（主叫，送出向）——终端 2
$env:TG_API_ID="A_api_id"; $env:TG_API_HASH="A_api_hash"
$env:TG_CALL_POC_SESSION="A_session_string"
$env:TG_CALL_POC_CONFIRM="i-understand-test-only"
$env:TG_CALL_POC_ROLE="call"; $env:TG_CALL_POC_PEER="号B的 user_id"
python -m tools.tg_call_poc
```

⚠ 两个测试号必须**空闲**（未被常驻 main.py 占用同一 session，否则 "database is locked"）；
本机 main.py(PID 常驻) 已用 camille_test + 8244899900，PoC 请用**另外**两个登录号。
判定：号 B 日志出现 `IN recv frames` 增长 → #44 已修、A 线通过、`transport_verified` 可置 true。
`SUMMARY` 落 `logs/tg_call_poc.jsonl`。

**投产前 dry-run 自检**（零连接零通话，含 main.py 常驻时可随时跑）：
```powershell
python -m tools.tg_call_dryrun
```
验证除 #44 外全部技术前提：① py-tgcalls 3.0 + ntgcalls 3.0 真机可 import + 构造 NTgCalls 绑定
（本机 cp313 **已实测通过**：`ntgcalls 3.0.0b2+dev.116ada7`）；② `build_call_runtime` 按真实
合并配置装配；③ 真探 176:7860 主机（**已实测 reachable、model_loaded=false**＝引擎未载入，
接通前需「启动引擎」）；④ 就绪度 blocker/warning + 接线 missing 清单。

## 传输层 B：tg2sip WebRTC 分支（备选，#44 不过时启用）

仓库 `tg2sipfork/tg2sip_tgcalls_webrtc`：Telegram↔SIP 网关，README 宣称支持
**tgcalls v0/v1/v2 全 call layer（7.0→13.0）** + libtgvoip 2.4.4，实测通过 iOS/Android/
Desktop/macOS/web.telegram.org 全客户端——**接通率证据强于 ntgcalls**。

**架构含义**：给人设号挂一台「专门接电话的虚拟设备」（TDLib 自持会话，与 pyrogram 消息
worker 并存互不干扰），把「追 Telegram 通话协议」这个最不稳定的活外包给独立组件，
我们的 AI 栈只面对 **SIP/RTP** 这个稳定几十年的标准接口。

**代价**：需 Docker（本机 docker-desktop 已装但 Stopped，用前先启）、社区小需自编译维护、
GPLv2（自用无碍）、多一跳 SIP↔RTP 转换（延迟 +若干十 ms）。

部署骨架（P0 备选，仅在 A 线闸门③不过时执行）：
```powershell
# 启 docker-desktop 后
docker build -t tg2sip-webrtc https://github.com/tg2sipfork/tg2sip_tgcalls_webrtc.git
# 用测试号 gen_db 登录 → 配 settings.ini 的 SIP callback_uri 指向本机 SIP 端点
# AI 侧起一个最小 SIP UA（pjsua/baresip）收 RTP → 桥到 core → 大脑
```
SIP UA 侧的 RTP 收发同样复用 `core` 的帧数学（`frame_bytes`/`FramePacer`/`split_pcm_frames`）。

## ⚠ P0 关键实测发现：真正的瓶颈是「嘴」，不是传输层（2026-07-14）

实施中实测了 cascade 方案的「嘴」（TTS 流式首包 TTFB），结论**颠覆纸面假设**：

| 端点 | GPU | 首包 TTFB 实测 | 结论 |
|---|---|---|---|
| 7852 `/v1/tts/clone/stream`（克隆声）| 本机 3060 | **48-63s** | 完全不可用于实时通话 |
| 7852 `/v1/tts/stream`（内置音色）| 本机 3060 | **7.4s** | 仍远超实时门槛 |
| 176 MiniCPM-o 实时 WS（S2S）| 5090 | ~1.3s（config 记载热态）| 唯一接近实时的路径 |

**根因**：本机 3060 是 AvatarHub 消息语音条的「在线主力」且显存长期吃紧（CLAUDE.md 有
OOM 事故记录）、模块级 GPU 串行锁——它**能做异步语音条（2-4s/句可接受），但扛不了实时通话**
（实时需流式首包 ~150-300ms 才有真人感）。传输层（ntgcalls 3.0）反而 wheel+API 齐备、风险低。

**这改写了「嘴」的选型**（原方案默认 cascade 用 CosyVoice 的假设不成立）：

1. **cascade 要成立，嘴必须挪到 5090 且走流式克隆**——但 5090 已驻 MiniCPM-o(22GB)+FLUX+MT+
   嵌入，显存极度紧张，再塞一个 CosyVoice3 常驻实例需重新做显存预算（可能挤掉出图/嵌入）。
2. **S2S(MiniCPM-o) 在 5090 上本就是流式克隆、~1.3s**——它同时是「大脑+嘴」，无需额外显存，
   是当前环境下**延迟最优**的路径。代价仍是：安全栈无法「出口前拦截」、克隆保真需过声纹探针。

→ **P1 第一优先级从「盲建通话桥」改为「5090 嘴的 bake-off」**（见下方修订计划）。
在真机延迟数据出来前，不预设 cascade 必胜——**用测量说话**。这是实施中的方案自我修正。

## 大脑选型：为什么 cascade 是情感陪护的真人感最优（非妥协）

电话真人感 = 音色连续性 × 内容连续性 × 节奏自然 × 情绪承接。

- **cascade（半级联，默认）**：流式 ASR + 既有 `generate_reply` 全链（人设/记忆/情绪策略/
  危机安全网/persona_guard/命理/场景状态**一行不改全量生效**）+ CosyVoice3 流式。
  - 音色：与消息语音条**同一个克隆声**（音色主权，不漂移）；
  - 内容：记忆/人设/安全栈都在，"记得你"+人设不崩+危机可拦（说出口前）；
  - 节奏/情绪：靠 `core` 的 `ThinkingFiller`（思考填充音）+ `BackchannelDecider`
    （倾听反馈）+ 副语言标记/变速（复用 avatar_voice 资产）补齐。
  - 延迟实测口径：流式 ASR ~300ms + smart-turn ~65ms + LLM 首 token ~500-800ms +
    CosyVoice 流式首包 ~150-300ms ≈ **1.0-1.5s**，与 S2S 相当。
- **s2s（MiniCPM-o 4.5 全双工，备选/AB）**：原生打断/backchannel，但克隆保真需过声纹探针、
  安全栈无法「出口前拦截」（音频直出）。仅作对照盲测与灰度实验，不作情感陪护主链。

**结论（纸面）**：S2S 只在「节奏/打断」单项有原生优势，却要牺牲音色主权 + 全安全栈——
对情感陪护是本末倒置。cascade 用现有护城河资产把真人感做到位，S2S 做 A/B 备胎。

### 实测修正（2026-07-14，实施中推翻纸面结论的关键一步）

纸面推荐 cascade 的前提是「CosyVoice 流式嘴够快」。**实测证伪**：本机 3060 上 CosyVoice
克隆流式 TTFB 48-63s、内置音色 7.4s——`cascade` 的嘴在当前硬件上**根本达不到实时**。
而 5090 上的 MiniCPM-o S2S 本就是流式克隆 ~1.3s。据此修正选型：

- **P1 现实最优 = 复用既有 `realtime_voice` 的 S2S 大脑**（MiniCPM-o @5090，已驻显存、
  ~1.3s、原生全双工），把原生来电传输（ntgcalls）经通话桥接上去——**零新增显存、延迟达标**。
  人设/记忆经 `build_call_system_prompt` 注入（已实现）；安全栈改用**转写并行监测**
  （host 出 `transcript.user` → 跑 `detect_crisis` → 注入安全指令 / 强制转人工 / 挂断），
  从「出口前拦截」退化为「一两轮内拦截 + 人工升级」——对语音危机可接受且有兜底。
- **cascade 升级为「未来态」**：需要一块**专职流式克隆嘴的 GPU**（CosyVoice3 常驻 5090 会挤掉
  出图/嵌入，需重做显存预算）才能落地。届时音色主权 + 全安全栈的理想架构才成立。
- **音色连续性风险**（S2S 的 MiniCPM-o 克隆 vs 消息语音条的 CosyVoice 克隆，同参考音不同引擎
  → 音色可能有差）：P1 必须真机 A/B 听测 + 声纹探针；若断裂明显，倒逼 cascade 提前。

→ 一句话：**先用 S2S 把「电话能打、够快、像真人」跑通拿数据，cascade 作为音色主权的升级路线
排进第二梯队，由 A/B 数据 + 专用 GPU 到位与否触发。**

## 风险登记

1. **账号封禁**：自动接听是明确 userbot 特征，风控烈度未知 → 测试号先行、日通话时长纳入
   account 健康信号、kill-switch 联动（`decide_incoming_call` 已接 `kill_switch_frozen`）。
2. **#44 进向静默**：3.0 有 API 但需实测；不过 → 转 tg2sip。
3. **协议漂移**：Telegram call layer 升级可能让库一夜失效 → 补偿路径（消息语音条）就是灾备。
4. **并发=1**：单卡单 MiniCPM-o worker → 忙线走 `DECLINE_COMPENSATE`（拒接+回拨承诺，
   绝不 ring-out）；起 INT4 双实例可提到 2。
5. **合规**（业务侧拍板，非工程）：AI 披露义务 vs「绝不承认是 AI」守则的冲突按管辖区定；
   通话转写留存涉录音同意法。
