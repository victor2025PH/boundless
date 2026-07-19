# platform/enable · 赋能能力网关契约（克隆音 TTS / 翻译 / 数字人渲染）

> 定位：承接中台（chengjie）在生成 AI 回复时，需要调用底层引擎的**赋能能力**——把回复转成
> 人设克隆音、翻译成对方语言、渲染数字人视频。这些能力天然长在引擎侧（TTS/数字人在
> **avatarhub**，翻译在 **chengjie 翻译栈**），platform 把它们的实现搬进来既不可行也不必要。
> 正确架构是：**能力在引擎就地完成，platform 只定义『契约 + stdlib 瘦客户端』消费其 HTTP 能力面。**
> 因此本层是 **契约 + 客户端**，不是搬代码，守住 “platform 不反向依赖 engines”。

## 1. 能力与归属

| 能力 | 谁做 | 怎么触达 |
|---|---|---|
| **人设克隆音 TTS**（回复文本 → 克隆音音频） | avatarhub（`_profiles` 角色档案 + 合成就地） | `POST /api/tts_only`（真实已在跑端点，2026-07-19 核实纠正） |
| **翻译**（回复文本 → 目标语言） | chengjie 翻译栈（`TranslationService.translate`） | `POST /api/translate`（第三阶段已接线，见 chengjie `src/web/routes/enable_routes.py`） |
| **数字人渲染**（文本 → 语音 + 可选口型视频） | avatarhub（"数字人开口"族，依托 `_profiles` 档案） | `POST /avatar/speak`（2026-07-19 第四阶段：路径与字段均已核实对齐，见 §5/§6） |
| **状态（可达性/能力开关）** | avatarhub | `GET /api/enable/status`（**已落地在跑**，2026-07-19，纯探针） |

> 设计含义：三个能力对承接侧都是**增强项而非必需项**——赋能面全部离线时，chengjie 仍能
> 正常发纯文本回复。这正是"赋能网关"与"硬依赖"的本质区别。

## 2. 契约（stdlib 瘦客户端签名）

`platform/enable/client.py`（纯 stdlib，零第三方依赖，零反向依赖）：

```python
EnableClient(avatarhub_url=None, chengjie_url=None, timeout=8)
  # avatarhub_url 缺省读环境变量 AVATARHUB_BASE_URL（缺省 http://127.0.0.1:9000）
  # chengjie_url  缺省读环境变量 CHENGJIE_BASE_URL（缺省 http://127.0.0.1:8080）
  .tts_clone_speak(text, profile_id, lang="zh", fmt="ogg")
                                  -> dict   # /api/voice_clone/tts → {audioUrl, ms, c2pa}
  .translate(text, to_lang, from_lang=None)
                                  -> dict   # /api/translate → {text, detected_lang}
  .avatar_render(text, profile_id="", generate_lipsync=True)
                                  -> dict   # /avatar/speak → {audio_base64, lipsync_video_b64, elapsed_ms, ...}
                                  # lipsync_video_b64 需 profile 已配 face_b64 才会非空，否则静默只回音频（见 §5/§6）
  .status()                       -> dict   # /api/enable/status（不可达则 available=False）
  .available()                    -> bool   # avatarhub 赋能面 HTTP 可达
```

- 请求/响应字段的完整 schema 见同目录 `enable_schema.json`。
- **不做能力实现**：客户端不含任何 TTS/翻译/渲染逻辑——那些只在引擎侧发生。

## 3. 依赖方向

`chengjie/products → platform/enable(契约+client) → (HTTP) → avatarhub/chengjie 能力面`。
platform 不 import 引擎代码，仅通过 HTTP 契约交互；能力实现与其机密（声纹档案、
数字人资产、翻译模型）留在各自引擎本机。

## 4. 可降级说明（引擎离线时承接侧如何优雅退化）

所有方法**绝不抛异常**：任何失败（HTTP 错误/超时/连接失败/JSON 解析失败）都收敛为
`{"available": False, "error": ...}`。承接侧（chengjie）的退化约定：

| 不可用能力 | 承接侧退化行为 |
|---|---|
| `tts_clone_speak` | 发纯文本回复（跳过语音） |
| `translate` | 直接发原文（或提示暂不支持翻译） |
| `avatar_render` | 只发音频/文本，不发数字人视频 |

隐私红线：`translate` 的原文只作为请求载荷发往翻译栈，**不落任何日志/事件**；
计量只用字符计数（`chars` 字段，见 schema）。

## 5. 落地状态与下一步

**2026-07-19 第三阶段更新（真实接线 + 契约纠偏）：**

- ✅ **`GET /api/enable/status`**：已在 `avatar_hub.py` 落地（纯探针，返回 `available`/`tts_ready`/`profile_count`）。
- ✅ **`POST /api/translate`**：已在 chengjie 落地（`src/web/routes/enable_routes.py`，包 `TranslationService.translate`）。
- ✅ **`tts_clone_speak` 路径纠偏**：首版契约臆测的 `/api/voice_clone/tts` 在源码里不存在；核实后改指向真实已在跑的 `POST /api/tts_only`，字段（`profile`/`audio_base64`/`ok`/`elapsed_ms`）已按源码对齐，**但 avatarhub 端点本身未新增代码，只是客户端瞄准了正确目标**——若要真正调通还需确认鉴权（`X-AH-Token`，见 avatarhub `_auth_middleware`）与 `_profiles` 里已有可用角色。
- ✅ **`avatar_render` 路径纠偏**：`/avatar/speak` 前缀已从授权闸门列表(`_GEN_BLOCK_PREFIX`)反推确认；字段第三阶段时**没有读源码确认**，已在第四阶段补全核实，见下方「2026-07-19 第四阶段更新」。
- 本层交付：`CONTRACT.md`（本文件）+ `client.py`（瘦客户端，字段已纠偏）+ `enable_schema.json`（字段契约，已标注纠偏与待核实项）。

**2026-07-19 第四阶段更新（`avatar_render` 字段核实对齐）：**

- ✅ **`avatar_render` 字段核实对齐**：逐行读完 `avatar_hub.py` 的 `SpeakRequest`/`SpeakResponse`/`avatar_speak` handler（`@app.post("/avatar/speak", response_model=SpeakResponse)`），并用文件内嵌前端 JS（`/ui`、`/mobile/ui`）与内部调用方 `_station_announce`（点歌播报功能）的真实调用样例交叉验证。核实结论与首版设想差异很大，`client.py` 已按真实字段重写：
  - `text` 恒为必填，**没有 `audio_url` 这条路**——`/avatar/speak` 是"文本→现场TTS→可选联动口型"一体流程，不支持喂入已合成音频跳过 TTS；首版契约"audio_url 与 text 二选一"的设想在源码里不存在。
  - `persona_id` 改名对齐为 `profile_id`，真实字段是 `profile`——与 `tts_clone_speak` 是**同一个 `_profiles` 档案**，不是另一套身份体系（旁证：创建档案时的埋点事件正是 `huanying.persona.created`）。
  - 数字人视频（`lipsync_video_b64`）是**可选且有前置条件的软依赖**，不是保证产物：需请求显式 `generate_lipsync=true`，**且**目标 `profile` 已经用 `PATCH /profiles/{name}` 配过 `face_b64`（人脸底图）——这正是任务背景里问的"前置装配状态"，只是它是软依赖（缺了不报错、`available` 仍为 true，只是静默拿不到视频），不是像授权闸门那样的硬阻断。
  - 响应体音频/视频都是内联 base64（`audio_base64`/`lipsync_video_b64`），**没有 `videoUrl`**；`elapsed_ms` 是处理耗时，不是"视频时长"。
  - `/avatar/speak` 与 `tts_only` 同受 `_license_gate_middleware` 管辖（`_GEN_BLOCK_PREFIX` 命中），试用/授权到期且强制模式时会先被 403 挡下；鉴权头同样是非回环来源需 `X-AH-Token`。
- ⚠️ **同族的流式/无口型批量变体不纳入本契约**：`/avatar/speak/stream` 与 `/avatar/speak/batch/stream` 返回 `StreamingResponse(text/event-stream)`（SSE 进度流，非一次性 JSON）；`/avatar/speak/batch` 是 JSON 但入参是裸 `List[BatchSpeakItem]` 且**完全没有口型/视频能力**（仅批量 TTS，`BatchSpeakItem` 无 `generate_lipsync` 字段）。三者都不符合瘦客户端"一次 POST 拿完整结果"的设计前提，本阶段不强行封装，留给专门设计（流式需 SSE/WS 客户端；批量需另开一个不同签名的方法）。
- 本层交付：`client.py` 的 `avatar_render()` 方法体与 docstring 重写、`enable_schema.json` 的 `avatar_render._note`/`request`/`response` 重写、本文件 §1/§2/§5/§6 同步更新。

## 6. 与既定方案的偏差记录（诚实记账）

契约先行的方法论价值在于："先写契约再读引擎源码"会暴露猜测与现实的差距，而暴露差距远好于silent 猜错。本轮暴露的两处：

1. `tts_clone_speak` 的响应是**内联 base64 音频**，不是 URL——说明 avatarhub 当前设计里不持久化合成产物为可下载文件；下游若要"发语音消息"，消费方要处理 base64 而非下载链接。
2. `avatar_render` 的真实端点名与字段仍未知——**宁可让契约显式标注"未核实"，也不要在没读源码的情况下假装已经对齐**；这是本次深度思考后主动收窄的范围（把"读清楚再接"排到第四阶段，而不是本轮硬猜完工）。

**2026-07-19 第四阶段补记**：上面第 2 项"仍未知"在这一轮兑现，暴露的差距比"路径对不对"更大：

3. 首版契约设想的"`audio_url` 与 `text` 二选一"**在源码里根本不存在**——`SpeakRequest`
   没有任何接收已合成音频的字段，`text` 恒为必填。"数字人开口"在 avatarhub 的设计里
   从来就是"文本进→TTS 现场合成→可选联动口型"一体流程，不是一个独立的"音频驱动口型"
   能力；契约层如果不读源码，很容易顺着直觉（"给一段音频就能渲染视频"很合理）猜出一个
   源码里不存在的输入方式——这正是本轮最大的教训。
4. "数字人渲染"这个契约名字对这个端点其实是"选配"——`generate_lipsync` 服务端默认
   关闭，即便打开也需要目标 `profile` 已经用另一个接口（`PATCH /profiles/{name}`）配过
   人脸底图（`face_b64`）才会真的出片，否则静默退化为纯音频、不报错、`available` 仍是
   true。端点的"本分"其实是语音合成（与 `tts_clone_speak` 共享同一套 TTS 管线），
   数字人视频只是其上可选叠加的一层。调用方若假设"调用 avatar_render 就一定拿到视频"
   会被这个静默退化坑到——本次在 `client.py`/`enable_schema.json` 里反复强调"必须看
   `lipsync_video_b64` 是否非空"，就是为了不让这个坑传染给下游承接方（chengjie）。
