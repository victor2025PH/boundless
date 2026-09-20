# 实施55：人设清唱能力（会哼两句）——hub 翻唱 dry_vocal × 预渲染 P0

> 2026-08-22。需求：客户点名「唱首歌」时，人设用**同一把克隆声**清唱一段（不要
> 带伴奏的音乐生成）。旧实况：`voice_autosend` 把「唱首歌」并进 `peer_requested_voice`
> 强制语音口径 → **说话声念歌词冒充唱**（实录穿帮）。
> 状态：**P0 代码全量 DONE-CODING**（默认关，随下个重启窗装载=零行为变化）；
> **PoC 与首批备货已完成**（176 于 11:42 回线后补跑）：首样四道客观闸全过
> （55s 转完 125.7s 源曲，hub 声纹 0.757 / clone_score sim 0.8275 / 自然度 0.824）；
> 首批模板 2 首（pd_twinkle 公版修剪 30s + origin_wind ACE 原创 29.9s——**全混音
> 直接走 cover dry_vocal，一步分离+换声+出干声**，stem 端点 400 的坑就此绕开）；
> 备货 4/4（lin_xiaoyu/chen_meiling × 2 曲，ogg 190~217KB，meta 带双声纹分），
> fresh-skip 复跑验证零 GPU。**待办＝老板人耳裁决 → 重启窗装载 → overlay 灰度开闸**。
> 人耳材料：`D:\chengjie-instances\zhiliao\data\assets\voices\<人设>\songs\*.ogg` +
> `engines/chengjie/tmp_song_probe/out/`（整曲样本）。
> 唱歌声纹标定首批 6 样：clone cosine 0.64~0.76（说话地板 0.80 不适用，坐实）。
> 编排器契约实测补记：`POST /api/services/ensure?name=singing&wait_s=30`
> （**查询参数**，JSON body 会 422）；引擎冷启 ~10s 自动拉起。

## 调研快照（2026-08 时点，决策依据）

- 现有 TTS 栈**不会唱**：CosyVoice3 论文明列 singing 为 limitation；IndexTTS2 纯语音。
  instruct「用唱歌语气」＝跑调念白，恐怖谷，**排除**。
- 零样本歌声转换（SVC）成熟：Seed-VC（2024 基线）→ R2-SVC/HQ-SVC（2025）→
  **YingMusic-SVC**（2025-12 SOTA，抗伴奏污染）；零样本歌声合成（SVS）：
  **SoulX-Singer**（2026-02，Apache-2.0，42k 小时，MIDI/F0 双控，SVS+SVC 双模型，
  推理 ~12G VRAM）——P2 动态歌词候选。
- **hub（幻声 176:9000）已部署整套歌声子系统**（本次最大发现，方案据此重构）：
  `/api/song/cover`（multipart：song 源曲 + profile 人设档 + `pitch=auto` 自动调门 +
  **`dry_vocal=true`＝只回清唱干声**；reference 缺省＝该档注册克隆音）、
  `/api/song/create`（ACE-Step 原创成曲，带 `profile`+`svc_swap`）、
  `/api/song/task/{tid}/stem/{stem}`（干声/伴奏分离）、`/api/song/library`、
  `/api/song/yield`（唱歌任务给聊天语音让 GPU）、`/api/clone_score`（声纹分）。
  且 08-22 04:20 AvatarHub 线已冒烟过一单：`[翻唱] pd_twinkle.ogg（调门+12）`
  profile=林小雨-智聊，38.8s，hub 侧声纹 cosine **0.6976**（history_id 412——
  唱歌声纹分显著低于说话地板 0.80，**地板需按唱歌样本另行标定**，勿直搬）。
- 版权（市场红线，2026-08 央媒/法制日报口径）：AI 翻唱商用＝词曲著作权 + 录音
  邻接权 + 声音人格权三重雷区。**供给只走：原创买断 / 公有领域 / ACE 离线原创**；
  产品层不接点名翻唱（词表刻意只认「要你唱」不认「唱某某的歌」）。

## 三视角结论（详版见 08-22 03:44 会话）

- **工程**：SVC 优于 SVS（音准继承模板≈零跑调）；**预渲染优先于实时**（运行时
  绝不现场合成——176 白天 VRAM 88% 高水位 + 单任务 ~39s）；五套既有设施直接复用
  （hub_fish.profile_map / 媒体验证纪律 / persona 解析 / send_media 语音口 / 媒体日志）。
- **市场**：差异化强（竞品无克隆声清唱）、传播点（晒「AI 给我唱生日歌」）；版权
  红线定死供给形态；变现钩子后置（先免费灰度读数据，与命理同节奏）。
- **用户**：同一把声音是硬不变量（声纹分+人耳双闸）；触发时机>曲目数量（点名要 >
  生日 > 晚安）；拒绝要诚实可爱，**绝不冒充**；避重防「就会这一首」。

## P0 已实施（本批全部 .py/配置，默认关；随下个重启窗装载）

| 件 | 路径 | 说明 |
|---|---|---|
| 纯函数核心 | `src/companion/song_stock.py` | manifest 加载（坏行跳过/路径消毒/重复 id 拒）；`detect_song_request`（保守窄口径，否定/自述去 KTV 不算）；`requested_song_scene`（生日/晚安）；`pick_song`（语言闸→避重→场景偏好→crc32 日级确定轮换）；`SongSendLedger`（JSON 账本：日上限/冷却/7 天避重窗，30 天剪枝，坏文件容错）；`song_gate_verdict`（**显式 0=关闸**，勿 `or` 吞）；`SongStats` + `singing_status_snapshot`（60s TTL 盘点） |
| hub 客户端 | `src/ai/singing_client.py` | cover 提交/轮询/取产物/clone_score/参考音 sha1；**产物三道闸内建**（Content-Type→magic→尺寸，媒体产物验证纪律）；仅 CLI 消费，运行时零 import |
| 批渲 CLI | `scripts/song_prerender.py` | 数据根契约（_data_root）×profile_map 人设×manifest 模板；新鲜度三键（模板 src_sha1 / hub 参考音 sha1（**换声自动重渲**）/ 产物在且达标）；**同档复用**（同 hub 档同模板只烧一次 GPU）；ffmpeg→ogg/opus 48k mono（无 ffmpeg 保 wav 警告）；meta 落 hub 声纹分+clone_score；单条失败不断批，exit 1/2 语义 |
| B 线接线 | `src/inbox/autosend_helpers.py` | `autosend_song`（gated）+ `_autosend_deliver` 短路：**kline 之后、image 之前**；命中备货→`orch.send_media(media_type="voice")`+`[唱歌]《题》♪首句` 镜像+媒体日志合流（防唱完失忆）；无备货/频控 → False 回落，**绝不退回念歌词** |
| 观测 | `src/web/routes/voice_routes.py` | `/api/voice/avatar-status` 增 `singing` 段（模板数/每人设备货/计数器；不随 avatar_voice.enabled 闸——灰度前可看备货进度） |
| 配置 | `config/config.example.yaml` `companion.singing` | enabled=false / daily_cap=2 / cooldown_hours=6 / repeat_window_days=7 / allow_lang_fallback=false（不给英文客户硬塞中文歌）+ dirs 覆写 |
| 模板库脚手架 | `config/song_templates/README.md` + `manifest.example.json` | 供给红线（origin/pd 两类 source 台账字段）、干声质量要求、渲染 runbook |
| 门禁 | `tests/test_song_stock.py`(29 例含上面全部纯函数)、`test_singing_client.py`、`test_song_autosend.py`（fake orch/store 全路径 + **deliver 链位序静态钉**） | 29 新例全绿；邻域回归（autosend_helpers/route_inventory/kline/deliver_once/voice_system_default）84 例全绿；example YAML 语法+配置门禁绿 |

实施中修掉的自打脸 bug（门禁抓的）：`int(raw or 2)` 把显式 0 吞成默认——与本仓
「daily_reply_budget=0=不限额」同款语义事故，已改 `_int_or/_float_or`；
选曲顺序改为**避重先于场景偏好**（场景曲被避重排掉时回落通用曲，不该整个不唱）。

## 事故记录：幻声机（176）宕机

- 时间线：11:08-11:10 网关正常应答（openapi/曲库/任务/档案全量探明）；
  11:11:46 三个 `/api/services/ensure` 探参返回 **422**（FastAPI 入参校验直接挡，
  handler 未执行，无副作用）；**11:11:50 前后整机失联**（9000 TCP 连接超时 + ICMP
  ping 100% loss）。当时 VRAM 28.8/32.6G（88%）。本线全部操作为只读 GET + 422，
  无引擎启停/无 GPU 任务提交——时间上邻近，因果上无通路，疑似自身故障或有人维护。
- 影响：聊天语音链按既有兜底降级（hub 质量轨→回落），非本线新增风险；
  PoC/备货渲染阻塞。已挂后台守望（90s 间隔 Test-NetConnection，回线即报）。

## 回线后 runbook（PoC → 备货 → 灰度）

1. **PoC 首样**：`python tmp_song_probe/poc_cover.py`（下载 history 412 当源曲 →
   dry_vocal 翻唱到陈美玲-智聊 → 四道客观闸 + clone_score 记录 → 产物落
   `tmp_song_probe/out/` 交人耳）。顺手实测编排器拉起引擎的正确端点，回填
   `song_prerender.wait_engine_online` 的探参清单。
2. **模板首批**：实例数据根建 `config/song_templates/`——pd_twinkle（公版，hub 上
   已有源）+ ACE 离线原创 2-3 首（`/api/song/create` style+lyrics_assist → 人工筛 →
   取 vocal stem 当模板）；manifest 按 example 填，source 如实。
3. **备货**：`python scripts/song_prerender.py --persona lin_xiaoyu --persona chen_meiling`
   （夜间低峰跑全量）；查 `/api/voice/avatar-status` singing.stock。
4. **人耳裁决**（老板/运营）：首批每人设逐条听——通过才进 5。
5. **灰度开闸**：zhiliao overlay `companion.singing: {enabled: true}`（热更 ~30s，
   无需重启——前提是 .py 已随重启窗装载）；重启窗 rider 验证：
   `pytest tests/test_song_stock.py tests/test_singing_client.py tests/test_song_autosend.py -q`
   （29 绿）→ `GET /api/voice/avatar-status` 出 `singing` 段。
6. **两周读数**：avatar-status singing.stats（requests/sent/no_stock/capped 分布）；
   投诉词表窗口人工盯（P1 再机制化）。

## 人耳归因轮（2026-08-22 13:54）——时间窗 + 漏乐，不是「电音」

老板听 `ear_round3` 四条的原话：A 16s 后没声 / B 12s 后二胡 / C 11s 后没声再出音乐 /
D「没有声音」。逐秒 RMS 对上：

| 文件 | 能量事实 | 根因 |
|---|---|---|
| A 晚风 turbo 干声 | 0–15s -11~-19dBFS，16s 起 -55~-69（死静音） | ACE 30s 片长，4 句词只唱前 16s |
| B 月光 turbo 干声 | **全程 30s 都在 -14~-33**，无静默 | 分离器把二胡漏进「干声」，能量裁剪看不到缺口 |
| C 月光精细干声 | 0–10s 人声，11–13s 空，**15–18s / 22–26s 伴奏灌回** | 精细分离仍漏尾奏；空窗是可切的 |
| D 陈美玲转换 | 峰值 0dBFS，前 10s **-10dBFS（很响）**，形状抄 C | **不是空文件**。11–14s 空窗 + 单声道 WAV 部分播放器静音，听成「没声音」 |

机筛当时给 D 打自然度 0.966——评分器整段归一化，空尾和漏乐都骗得过。
**尺寸 / HTTP 200 / clone_score 都不构成「这是一段完整清唱」**（媒体产物验证纪律的歌声版）。

已落地（生产函数，不靠探针对脚本）：
`song_stock.prepare_dry_vocal`＝静默终止 + 相对跌落（抓 B 这种「人声没了乐器顶上」）
+ 歌词时长硬帽 + 峰值归一 -1.5dBFS + 立体声镜像。`song_prerender` 送 hub 前与收
成品后各跑一次。门禁 `test_prepare_dry_vocal_*`。人耳材料：`tmp_song_probe/ear_round4/`。

## 归因终局（2026-08-22 14:09 人耳裁决）——SVC 转换环节＝假声唯一来源

老板听裁剪版后的原话：「A/B/C 这三个听着可以，**其他的都不行，太假了**」。
A/B/C＝ACE 源唱手干声（未转换）；「其他」＝所有 SVC 转换成品。历轮全部投诉
（颤音/电音/太假）都发生在转换后——**源没问题，YingMusic-SVC 换声环节是根因**。

证据链（ear5 实测，2026-08-22 14:14）：
- 转换代码（song_studio_server `run_svc`）：Seed-VC 系（whisper 语义 + campplus
  风格嵌入 + RMVPE F0 + CFM 扩散），**参考音是 21s 说话声**（voice_refs），拿说话
  的风格嵌入硬套唱歌＝「音色对但唱法机械」的结构性来源；`inference_cfg_rate=0.7`
  服务端写死，客户端只有 diffusion_steps(≤100)/pitch_shift 两个旋钮。
- steps=100 + pitch=0 + 干净裁剪源（转换路天花板样本，`ear_round5/*_100步.wav`）：
  声纹 cosine **0.8228**（月光，超说话地板 0.80）/ 0.7657（晚风）——**音色维度
  转换是成功的**，假在唱法/韵律维度（campplus 测不出，与 Phase10 结论同构）。
- 源声 vs 人设声纹：cosine 仅 0.11~0.26（机器口径「不是同一个人」）——源声直接
  冒充人设有被识破风险（客户天天听人设 TTS 说话声）。
- hub RVC 盘点：67 个现成模型全是名人/游戏角色/通用声（丁真/奥巴马/原神/女声-XX），
  **无智聊人设模型**；无离线批量 RVC 接口（只有实时麦克风 + 6s 试听）。per-persona
  RVC 训练（可用人设 TTS 合成语料训）＝AvatarHub 线的活。

三条路（呈老板裁决）：
| 路 | 音色 | 自然度 | 成本 |
|---|---|---|---|
| ① SVC 转换（现状） | ✅ cosine 0.77~0.82 | ❌ 人耳「太假」（steps100 待复听） | 已到客户端参数天花板 |
| ② 声选角（ACE 直出，不转换） | ❌ cosine 0.1~0.3（提示词+抽签能拉多近待实验） | ✅ 人耳已认可 | 每人设一次性选角成本 |
| ③ 引擎升级（AvatarHub 线） | ✅ | 预期✅ | per-persona RVC 训练 / SoulX-Singer 部署 / cfg_rate 暴露，周级 |

产品语义护栏（若走②）：唱歌嗓≠说话嗓对真人也成立，但差距要「像同一个人的唱歌嗓」；
选角声纹地板按人耳标定（候选实验 `tmp_song_probe/casting.py` 读数见 ear_round5）。

声选角首批读数（2026-08-22 14:21，2 提示词 × 1 seed，20s turbo）：
- 暖女声 vs 陈美玲 cosine **0.3448**（nat 1.0）——比既有源基线 0.19~0.26 高，
  提示词能拉近；vs 林小雨仅 0.168。
- 甜女声 vs 陈美玲 0.2566 / vs 林小雨 0.1775——「甜」提示词没贴上林小雨，
  她的选角提示词要重写（或多 seed 抽签）。
- 结论：② 的声纹上限量级 ~0.3-0.4（vs ① 的 0.82），能不能过人耳「像同一个人的
  唱歌嗓」由老板听 ear_round5 裁决；若过，P0 供给改「每人设选角 N seed 抽签 +
  cosine 排名 + 人耳终审」，song_prerender 的 SVC 步对选角模板改为直存。

## 人耳裁决 ear5（2026-08-22 23:52 老板原话）——路线定局

「候选嗓1_月光_暖女声 / 候选嗓2_月光_甜女声 **这两个可以**，但是没有唱完，突然
结束了。陈美玲_晚风_100步 / 月光_100步 **还是很假**。」

⇒ **① SVC 转换路正式退役**（steps=100 天花板样本仍被人耳判假；声纹 0.82 挽救
不了唱法机械）。**② 声选角路人耳过关**——剩余问题只是 20s 画布顶墙没唱完。
坑：casting.py 当时没传 seed（result.seed 只回显请求值＝null），ACE 任务 TTL 6h
已清——**认可的两把嗓子无法按 seed 复现**，只能基于已有 17.31s 认可音频续作。

## 补完轮 ear_round7（2026-08-23 00:04-00:31，`extend_round.py`+`extend_sweet.py`）

方法＝**repaint 续唱**：认可 wav 垫静默到 24/28s 画布 → `/v1/repaint` 窗
[14.75(第四句前换气口), 画布尾]，同 prompt/词、retake_variance=1.0（0.85 实测被
静默 latents 拽回寂静）→ mel 分离取 vocals → **原音拼接**（窗前逐采样保留认可
音频，换气口 0.1s 交叉淡化）→ prepare_dry_vocal。四道闸：RIFF → 自然收尾（人声
止于尾前 ≥1.5s）→ **ASR 四句在场**（176:8765 large-v3-turbo，逐句局部相似度）→
**campplus 相似度 vs 认可样本**（本机 D:/faceX/mfys clone_scorer，只测续作段）。

| 产物（ear_round7/） | 来源 | 收尾 | 四句 ASR | sim vs 认可 | 自然度 |
|---|---|---|---|---|---|
| 暖女声_月光_完整版 | repaint seed=912226340 窗14.75-28 | 自然(19.0s) | 0.88/0.62/0.57/0.83（转写逐字全对） | **0.889** | 0.972 |
| 甜女声_月光_完整版 | repaint seed=2147133273 画布24 | 自然(19.1s) | 0.88/0.88/0.86/0.83（「猜」听成「在」） | 0.837 | 0.879 |
| 甜女声_月光_备选_整曲连唱 | 28s 整曲新抽 seed=2000350194（并发孤儿件） | 自然（末音衰减完 21.4s 收刀） | 逐字全对含末句 1.0 | **0.908** | 0.835 |

备选件人耳返工实录（00:48 老板「22 秒以后出现怪音和男声」）：ACE 在歌词唱完
（verbose ASR 末句止于 18.3s）后的空画布上**自行填了别的声**——21.5s 起怪音/
男声，我的四道闸全瞎（四句都在前 20s 全对、campplus 整段平均分被稀释不塌）。
修剪两连坑：① 按 ASR 末句时间戳(18.3s)+1.5s 窄窗收刀 → 切在延音中段（ASR 标
的是词尾不是声学尾）；② 正解＝末句后的**声学谷**（-39dB @21.0s）+0.4s 收刀，
延音全保留、杂声全去。从 RAW 微斯重分离重建（v1 已把成品覆盖掉——修剪前先
备份原件，这条也记进坑）。**闸升级待办**：备货 CLI 的成品闸补「歌词唱完点
之后 ≥1.5s 处仍有新的有声段＝画布填充污染」判定（verbose ASR 末段 end vs
能量扫描交叉），防同款漏网。

甜女声 repaint 首试翻车实录：raw 能量看窗内「有声」13s，**分离后归零**——ACE 把
窗填成了器乐（媒体验证纪律的又一变体：能量 ≠ 人声，闸必须设在分离之后）。

**176 显存编排三坑**（本轮实锤，后续批渲都会踩）：
- `ace_studio` 任务后 torch 缓存 **~10.9G 不回落**，且 `_ensure_vram` 用
  mem_get_info 自查 → **自己挡自己**（「显存不足 10.9G<11G」实为自家缓存）。
  解法＝阶段间 `POST /api/gpu/park?name=ace_studio`（parkable，ensure 即复活）。
- hub `free_unused` **不碰 ollama runner**（qwen3-vl 5.1G + hy-mt2 4.7G 夜间也会
  被产线拉回）：ACE 前直接 `/api/generate {keep_alive:0}` 卸载，用完自动回来。
- 杀后台批任务要杀 **python 子进程**而非 PowerShell 壳（本轮杀壳留下僵尸并发
  烧卡+互抢 VRAM，孤儿产物混进 ear7——事后按闸补验才敢收编为备选件）。

**人耳裁决 ear7（2026-08-23 00:54 老板）：「现在可以了」**——三条全过（备选件
经 00:48 修剪返工后放行）。老板新指令：「怎么能让这些人设都可以会唱歌」→ 全员化。

## 全员化夜批（2026-08-23 01:00，`scale_round.py`；13 人设＝8女+4男+1客服号）

**一、供给注册（已落生产数据根 zhiliao）**：
- SVC 旧货 8 件（chen_meiling/lin_xiaoyu × origin_wind/pd_twinkle .ogg+.json）
  归档 `tmp_song_probe/retired_svc_stock/`——人耳判假的材料绝不留在货架；
- manifest：**origin_moon（月光）enabled**（词=月亮爬上了窗台…，source=origin，
  ACE 原创+自写词零版权雷）；pd_twinkle/origin_wind **暂 disabled**（旧货已撤、
  重供未到——enabled 而无货＝pick_song 选中后 find_stock None＝白落空）；
- 认可成品 → ogg/opus 48k mono 铺货（sidecar 带 `supply:casting_direct` +
  take seed + sim；**警示：song_prerender 的 SVC 分支会把这些当 stale 覆盖，
  CLI 直供分支落地前勿跑该 CLI**）：
  暖女声 → chen_meiling / warm_companion(小灵) / haruko_traveler(晴子) /
  su_wan(苏婉) / mizuki(美月) / professional_support(小界)（温柔/沉稳组）；
  甜女声 → lin_xiaoyu / lin_jiaxin(林佳欣)（俏皮/撒娇组）。
  同组同音频=既有「7 人设 2 音色」说话声先例的唱歌版；客户跨人设撞同曲同声
  的风险与说话声共享同级。
**二、男嗓选角**（marcus_wei/zhao_laoshi/zhang_jingguang/chen_mo 无声可用）：
  沉稳/清亮 2 提示词 × 2 seed，统一收尾闸，产物进 ear_round8 待老板耳。
**三、换词保嗓实验**（repertoire 解锁判定）：audio2audio（`/v1/create` ref_b64=
  认可成品，strength 0.55）× origin_wind 歌词——若 campplus vs 认可样本 ≥0.75
  且 ASR 词对 ⇒ **同一把嗓子唱任意新歌**成立，每人设 N 曲的供给就是纯批量；
  不成立则每曲每嗓独立抽签+人耳（慢但可行）。产物同进 ear_round8。
**统一收尾闸**（00:48 事故机制化，今晚全部产物过闸）：verbose ASR 末句词尾 →
  其后 3s 内最深能量谷 +0.4s 收刀+淡出——「画布填充污染」（ACE 在词尽后的空
  画布上自行填怪音/男声）在闸内根治；该闸待移植进 song_prerender/CLI。

**夜批读数（01:23 收）**：
- 女声供给 8/8 落库并经 `singing_status_snapshot` 只读验证（templates=3、
  stock 8 人设各 1、全 OggS）；
- 男嗓选角双双到手（ear_round8 待老板耳）：**沉稳** seed=2139757640（四句
  0.88/0.88/0.86/0.86，nat 0.939，**34s 画布**——28s 画布 5 连败＝慢板男声
  四句摆不进，画布加长即过，slow 提示词配 34s 是配方常数）；**清亮**
  seed=917935996（四句全过 nat 0.903）。四个男性人设 style 全是沉稳，缺省
  建议沉稳嗓全覆盖、清亮备选。
- **换词保嗓（repertoire 解锁）机制成立**：audio2audio（ref=认可成品，
  strength 0.45~0.55）× 新词 5 试，**音色全部保住**（campplus vs 认可样本
  0.81~0.89），但词保真是抽签（每试总有 1-2 句弱/丢）——晚风重供走「批量抽 +
  全四句 ≥0.5 硬闸」自动筛，**不惊动人耳直到 supply 级**；实验件已归档
  cast_raw/wind_demo_*（刻意不给老板听半成品）。
- 显存编排贯穿全夜（clear_for_ace：ollama keep_alive=0 + park singing/sbv2/
  ace）；偶发首试仍撞 5.9G（产线夜里也会拉回 vision/MT），重试即过——批工具
  化时该舞步进 CLI。

**上线开关（代码全备，两步）**：① 重启窗（次窗 04:00±45）装载 P0 .py——重启
前 `restart_preflight`，rider 验证 `pytest tests/test_song_stock.py
tests/test_singing_client.py tests/test_song_autosend.py -q` +
`GET /api/voice/avatar-status` 出 singing 段（stock 应显示 8 人设各 1；
01:20 实测运行中进程起于 12:28、代码 14:42 落盘＝**未装载，非重启不可**）；
② zhiliao overlay `companion.singing: {enabled: true}`（热更 ~30s 免重启）。
灰度读数看 avatar-status singing.stats（requests/sent/no_stock/capped）。
男嗓老板拍板后同法铺 4 男性人设即全员覆盖。

- 点歌台/点名翻唱（版权+技术+预期管理三输；词表刻意不认「唱某某的歌」）；
- 带伴奏成品歌（需求明确排除；清唱才像「真人随口哼」）；
- 运行时现场合成（GPU/延迟/176 白天高水位）；
- TTS instruct 假唱（跑调恐怖谷，负资产）；
- 无备货时的任何冒充（含现状的念歌词——短路不命中就回落正常文本链）。

## 下阶段

- **P1**（读数驱动）：生日/晚安 ritual 场景触发（milestone/daily_ritual 挂点）；
  draft 3b2「唱不了别承诺唱」hint（与 media_coherence_hint 同缝）；
  `wants_media`/promise guard 增 song 兑现语义；media_feedback 加 song 臂
  （回复率反哺发送概率）；投诉词表扩「不像你的声音/假唱」；ops 卡（等 ops_overview
  热区空窗）；英文模板批；唱歌声纹地板标定（首批 20+ 样本分布 → 定 floor 进 CLI 硬闸）。
- **P2**：动态歌词（SoulX-Singer SVS：固定旋律 MIDI + LLM 按字数格律填词 + 人设
  参考音零样本——名字/共同记忆唱进歌里）；定制点唱 SKU（下单→夜渲→人审→送达，
  catalog 模式同 bazi_reading）；变现开闸（免费灰度数据好 → 会员/单点）。
