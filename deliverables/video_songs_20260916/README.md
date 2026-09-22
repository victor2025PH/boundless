# 营销歌产线 · 智聊教学集（2026-09-16）

## 2026-09-17 P 系列「谁在用智聊」（人群 × 处境，取代 R「客户关系养成」功能线）

老板 09-17 03:09 定性：R1–R4 全是店长人设、拿一张图来回切，不是使用场景。P 系列一集一种人，一条完整因果链
**外语来信在镜头里到达 → 翻译 → AI 拟稿 / 手打 / 克隆声 → 发出 → 对方语言可见**，录屏按真实动作切、不循环。

| 件 | 作用 |
|---|---|
| `scenarios_persona.json` | 人群模型 P1–P8（persona / 岗位 / 企业 / 平台 / 语言对 / 触发 / beats / 音乐调色板 / utm）+ 两方舞台 `stages` |
| `persona_stage.py` | 两方真会话：客户侧账号（Katie 8244899900）经 `/api/unified-inbox/send` 真发外语 → 卖家账号（智聊支持）入站左侧异色；`--clear` 两侧清场（both_sides，否则 sync 会把上一集串回来） |
| `record_persona.py` | 事件驱动录屏：`incoming` / `xlate_quick_on` / `ai_reply_send` / `type_send` / `voice_preview`(open/type/gen/send 四相位) / `urgent_filter` / `narr`；每步等 DOM 真变化才算完成，落 `markers` 时间戳；`window.confirm` 自动接受；语音试听经 API 拉回验 magic 落盘 |
| `make_persona_episode.py` | B 模式拼装：人物卡 3.2s + 副歌起唱 → 录屏按标记切景（1280×720 取景框 1.5×：chat / low / list / panel）→ edge-tts 念白（按人物选声，过长自动加速）+ 底床 + 克隆声混入 + 来信 ding → 尾卡；卡顿 >20s 留头留尾剪中段；同一段表出 16:9 与 9:16（竖屏中段 1080×1080 直裁聊天栏不放大） |
| `gate_persona.py` | 语义闸：必拍动作齐且成功 / 两方气泡 / 译文行 / 无连接页空态 / 成片双流分辨率 / **帧多样性 aHash**（中位 <6 = 一张图来回切 FAIL）/ 念白条数 / 副歌底床 / 克隆声混入 / 公域禁词 |
| `test_scenarios_persona.py` | 模型完整 + 岗位不重复 + 首批 curriculum 含真实动作 + 禁 `open_nth_chat` + wait ≤3s + 副歌词闸；`--live` 加舞台探针 |
| `copy/persona_batch1.md` | 首批文案包（人物/处境/结果三件套 + utm 按人群） |

| `publish_persona_web.py` | 官网发布版出厂 + 上传 + 上架：按成片 meta 的 `zoom_timeline` **确定性**定位会话列表做脱敏（不靠模板匹配）→ 裁 6 s 尾卡 + 淡出 → H.264 High@L4.1/faststart/AAC 48k → 「谁在用智聊」海报 → magic/moov/ffprobe 双验 → scp 到 `/var/www/media/scenarios/chatx/` → `POST /api/admin/feed`（`ai=false`，默认不广播；payload 走文件避开 shell 引号） |

成片（各带 `_v.mp4` 竖屏）：P1 阿杰 独立站卖家·西语 / P3 Lin 客服主管 / P4 美玲 华人美容院·克隆声 /
P5 Mia 带货主播·工作链 / P8 老板本人·档位切换。五条横屏发布版已上线 `/videos`（`/media/scenarios/chatx/`）。
复跑：`python persona_stage.py --stage TG_ES --probe && python record_persona.py P1 && python make_persona_episode.py P1 && python gate_persona.py --only P1`。
R1–R4 成片保留在 `out/R*/` 供对照，不再投放。

**2026-09-18 第二批 P2 / P7**（新原语与纪律）：

- `use_stage`（一集跨两平台：P2 TG 阿语 + WA 英文，`scenarios_persona.json` 集级 `stages` 清场两处）、
  `list_filter`（列表搜索框过滤，`list_clean` 取景出厂不打码——来信未到时 0 行是预期，行随来信长出）、
  `kb_answer`（回复框 `/关键词` → 斜杠面板 → 知识条目带入 → 发送）。
- **演示数据录前建、录完删**：P7 `kb_demo_entry`（source=user 知识条目）、P2 `persona_demo`（导入 `demo_zhou_b2b`
  人设 + 会话级绑定两舞台；不绑则卖家账号自带人设「烧烤店老板娘」会答「你找错人了」——六录实锤）。`--keep-kb / --keep-persona` 重录省一步。
- **舞台产物隐藏**：`hide_staging_artifacts` 注 CSS 藏 `#cs-band/#cs-note`（「对方在永不自动回复名单」红字）与客户侧镜像行；goto 后重注。
- **失败即弃**：`incoming` 没落地 / AI 回复挂红字「发送失败」且重发未恢复 → `ClipAbort`，不出坏 take；
  WhatsApp 通道偶发「服务未启用」503（同刻 API 直发 ok）→ `customer_send` 5xx 重试 3 次、出站失败点【重发】、「逐条发送」队列停滞手动补发。
- 英文上架文案取各集 `en` 块（`test_scenarios_persona` 闸：四字段齐、不得夹中文）；`publish_persona_web --meta-only` 只改文案不重传。

**2026-09-18 P6 社群运营**（舞台 `TG_GROUP` = 公开群 `t.me/sqlt2026`「2026社群聊天」，卖家 智聊支持 / 客户 Katie 群内真发）：

- 新原语 `conv_scope`（切【群组】/【私聊】；切回私聊顺手清搜索词，否则私聊舞台行被搜掉）；`incoming` 在群舞台先等 8 s 直播入站，
  没有再 `from_latest` 兜底（`pull_group_latest` 接口成功即停，**禁止** sync-history——它会把各会话同一句旧询价再灌一遍，画面「消息都一样」）。
- **群实时入站的真根因不是触发闸**：A 线 `handle_group_message` 先过 `telegram.group_reply.allowlist_chat_ids` 灰度白名单，
  不在名单整条静默丢（debug 级）。实例 `config.local.yaml` 已加 `-1004309455763`。其后引擎改动（`ChatX` 待随版发）：
  未触发自动回复的群消息也镜像进收件箱（`telegram_client._mirror_untriggered_group_message`，开关 `group_reply.mirror_untriggered` 缺省开），
  触发路径的群镜像改用群名当会话名（此前 @本账号 的消息会把群会话改名成发言人）。`test_group_live_ingest.py` 三条互异样本 8 s 内全部 `live` 落地。
- 剧本：群里印尼语问 20W GaN 快充头（不 @ 任何人）→ 一键翻译 → 切私聊，买家跟进 → 演示人设 `demo_along_community`（报价口径写死数字，
  禁「稍后查一下」类拖延话）AI 报价逐条发出。拼装：Playwright VP8 关键帧极稀，先转 1 s GOP H.264 再按标记切；文件比 `end_t` 明显长时按比例拉伸时基。
- 发新版到频道：feed 广播幂等（有回执不重发），换视频须 `deleteMessage` 旧帖 + `DELETE /api/admin/feed?id=` 后 `--upload-only --broadcast`（09-18 183 → 186）。

**2026-09-18 晚 首批 P1/P3/P4/P5/P8 按新纪律重录**（四次 P1 take 沉淀出三条舞台准备纪律，全在 `clear_stage` 时自动做）：

- **主题钉**：工作台主题是坐席级漫游偏好（`/api/workspace/prefs` appearance.night.mode），19:50 有人在自己浏览器切亮色，
  录制新 context 也被拉成白底（竖屏 aHash 中位 3 → gate FAIL）。`chatx_session.session` 现在所有站内导航走 `url()` 带 `?theme=dark`
  窗口级钉（产品给嵌入端的机制：不写 localStorage、不碰坐席偏好），首屏主题≠钉打 WARN。
- **出站语言钉**：`set_outbound_lang` 把会话「发→X」钉成剧本客户语言（`lang_pair` 源语；`ar/en` 多语种不猜，除非集里显式 `out_lang`）。
  不钉时目标语靠 `conversations.language` 投票，短西语句「Perfecto. ¿Cuánto tarda…」被判 en → 中文回复译成英文发给西语客户。
  引擎侧同时给 `detect_language` 加 `¿ ¡` 西语标点信号（`test_detect_language_consolidation`）。
- **档位归位**：`set_automation_mode(review)`——上一集 `type_send` 留下的「人工模式 · AI 不拟稿不发 / AI 已让位」横幅不带进下一集。
- **AI 草稿逐条发送带 `source_lang`**（引擎前端 `_sendDraftParts`，`test_dpick_send_source_lang`）：smart-reply 按 reply_lang 把正文写成西语，
  再过出站翻译时服务端自检源语言把「Sí, enviamos a Chile…」判 en → 「译」成英文发出。现在草稿语言随发，同语种直通。
  演示人设 P1/P3/P5 另加 `language_follow=false`（防御性；正文语言最终由 lang_policy 决定，不是人设）。
- 群舞台 `incoming` 记 `via=live|pull|resend`，gate 对非 live 记 WARN（兜底补拉不许把入站链的病吃掉）。
- 引擎：群白名单拦截 debug→INFO（同群 1h 一次，提示往哪个键加群）；`ingest_incoming` 护栏——群入站 `name`＝发言人名时不作会话名
  （`test_group_display_name_guard`）。

**2026-09-19 F 系列教学片 F1「全自动回复：怎么开，怎么停」**（`format=tutorial`，七段 ~5:50，舞台 TG_ES + TG_GROUP，人设 ref P1；文案 `copy/tutorial_f1_autoreply.md`）：

- 与 P 系列的差别：一条主线讲透一个功能，观众跟着做。新原语 `say`（字幕条 + 念白同刻）、`wait_outbound`（**不点任何发送**，等引擎自己把回复发出去，
  记 API 正文 / 原稿 / sent_by）、`click_takeover`（点【接管】→ 等按钮变「交还 AI」+ 档位下拉刷成手动）、`assert_quiet`（让位后 N 秒无新出站气泡）、
  `ai_diag`（开【AI 状态】面板）、`ensure_mode`（API 直钉本会话档位，单段补录用）；`kb_answer` 加 `send=preview`（斜杠带入即清空，不发）；
  演示块可 `{"ref": "P1"}` 复用另一集（`resolve_demo_ref`）；`--only` 支持逗号分隔多段。
- **gate**：`check_auto_reply`——`wait_outbound` 段内不得有人工发送动作；出站正文须是客户语言（`out_lang=es` → 非 CJK）；
  `auto_reply_gate.any` 至少命中一项口径数字（39.9 / 7–10 / PayPal…）、原稿不得含 `forbid` 拖延话；有接管必有静默对照；群舞台段不得出站。
  单测 `test_gate_persona_auto.py`；`test_scenarios_persona` 加教学片静态闸（duration_range / gate 口径 / kb preview / 人设不得把口径写进不进 prompt 的 `style_hint`）。
- **真发前提（引擎侧，两处都进了 09-19 12:23 / 13:33 的 zhiliao 重启）**：
  1. `inbox.peer_bot_guard.never_auto_reply.exempt_peers`（实例 `config.local.yaml` 含客户侧 `8244899900`）——两方都是本租户账号时硬名单把对端当同事拦掉 A/B 两线；
     只豁免「作为对端」一侧，不豁免本账号镜像会话；`reply_diagnosis.managed_peer` 黄牌同口径（`test_never_auto_reply_exempt_peers` / `test_reply_diag_managed_peer_exempt`）。
  2. **接管迟到闸**（`telegram_client._takeover_abort` / `_aline_direct_send_allowed`）：A 线档位闸原只在生成前过一次，生成 + 拟人延迟十几秒内坐席点【接管】
     → 在途回复照发（12:50:56 接管，12:51:08 AI 仍发出折扣报价）。现 humanize 后 / 分条间复读档位，非全自动即弃发（回滚冷却记账 + 撤 dup-guard）。
     13:36 实录：13:36:07 接管 → 13:36:22 `A线让位（迟到闸 post_humanize）`，`assert_quiet` 8s 出站 3→3。`test_telegram_takeover_late_gate.py`。
- **人设口径要进 prompt**：`style_hint` 不是 `persona_manager` 消费的字段，写在那里等于没写（首录 AI 报「8–12 天」）。店铺数字放 `background` + `context.specific_memories`；
  `max_reply_sentences=3`。
- **拼装**：Playwright 录屏容器时基比页面墙钟长 6%~18%，此前按比例拉标记（同步对、动作 0.85× 慢放）；现 `conform_to_wallclock` 用 `setpts` 把画面压回墙钟
  （`_seekrt.mp4`），六段少 36s、动作真速。`wait_outbound` 卡顿阈 (20, 6, 9)：中段是真等待（拟人延迟 10~54s），观众不必陪等。
- 发布：`publish_persona_web` 海报眉题取 `card_label`（「智聊教学 · 全自动回复」），条目日期取 `feed_date`（09-19，排首批前）。：教学集 = 开头唱 + 说唱教学 + 逐字卡拉OK + 真录屏（不露脸、不用数字人）

**2026-09-19 F2「把微信接进智聊：三步，不碰密码」**（`format=tutorial`，五段 3:09（官网版裁尾卡 3:03），舞台 `WX_DEMO`，人设 `demo_xiaozhou_wx`；文案 `copy/tutorial_f2_wechat.md`；
官网 `chatx-f2-wechat.mp4` 条目 `persona-f2`，频道 msg 191）：

- **舞台没有第二个可编程账号**（微信 ≠ TG 有 Katie）：客户来信走副驾同一条入站桥 `POST /api/desktop/ingest`（与真副驾 UIA 读到新消息后落库同一函数
  `ingest_incoming`），落地/未读/SSE/拟稿全是真引擎行为，被替代的只有「客户在另一台手机上打字」。`customer_mode=desktop_ingest`，`incoming` 记 `via=desktop_ingest`
  （gate `EXPECTED_VIA`：这是设计不是兜底）。会话 `ephemeral=true`：每段录前 + `_cleanup` 都 `delete_stage_conversation` 硬删（带 tombstone，不被 sync 复活）。
- **老板实例的档位不能被教学片带走**：`wx_tier` 真点【保存】改半自动前先 `wx_policy_get` 快照到 `out/F2/_wechat_policy_snapshot.json`；`wx_restore`（d_verify 段尾）+
  `_cleanup` + 下次 `main` 开头三处还原（快照存在即还原后删）。gate `check_wechat_connect`：`wx_tier saved` 必有 `wx_restore restored`，否则 FAIL。
- **红灯即停**：`wx_env` 体检卡任一项非绿 / 「微信窗口不可见」→ `ClipAbort`（副驾 `state=blind`＝微信最小化，录出来就是错的教程）。`wx_verify` 等副驾 `online`；
  `wx_test_inbound` 注入后等验证卡 `#cg-recent .cg-recent.got`（容器类不变、内容翻卡）。引导页环境全绿 1.6s 会自动跳第 2 步（`autoAdvanced`）——`cg_step 1` 先让它跳完再点回。
- **档位预钉**：账号级决策是 `auto_ai`，不钉会被当全自动 17s 内出站。d_verify 先 `ensure_mode manual`（测试消息不起稿），e_inbox `ensure_mode review` 后再追问起稿。
  全片零出站（gate：任何段出现 `type_send/ai_reply_send/wait_outbound` 即 FAIL）；稿核 `draft_gate.any`（89/168/顺丰/明天）+ `forbid` 拖延话。
- **引擎侧真 bug（随版发，已进 restart 意向）**：`auto_generate_draft` 先把稿停泊成 `enriching` 并发 `draft_created`，人设正文 8~20s 后翻 `pending` 再无事件——
  工作台收到 `draft_created` 时拉 `status=pending` 为空把草稿条收起，开着会话的坐席永远等不到稿，切走再切回才出现（首录 e_inbox 75s 超时）。
  修：`drafts._publish_draft_ready`（enrich / release 收尾翻 pending 时发 `draft_ready`，不复用 `draft_created` 免得 webhook L2/L3 提醒推两次）+ realtime 白名单 +
  前端同路径刷草稿条 + 旧引擎兜底 `_cdraftRetryLater`（6/12/20/32/48s 退避补拉）。`test_auto_draft_persona_enrich` 加两条。录制侧 `wait_draft` 同时兜底：API 见 pending 而
  UI 无条 → 重开会话，记 `nudged`，gate 打 WARN（引擎重启前本片 e_inbox 就是靠它出的稿）。
- 拼装：`zoom_guide`（1280×720 @ (400,100)，居中向导卡）；`STALL_BY_OP` 加 `wait_draft (14,4,8)` / `wx_test_inbound|wx_verify (12,4,6)`；`LIST_RECT[zoom_guide]=None`（引导页无会话列表不打码）。
  `hide_staging_artifacts` 再遮 `.cg-recent .prev`（验证卡「最近一条」预览可能是老板的真消息）与 `.tko-pill.handoff`。

| 件 | 作用 |
|---|---|
| `curriculum.json` | **智聊功能应用清单**（13 项：收件箱/渠道/翻译/AI 拟稿/人设/知识库/克隆语音/工作目标/关怀记忆/小智/护栏/充值/安装）→ 教学章节 E1~E12 + T1；每章带 `footage_actions`（录屏动作 DSL） |
| `chatx_session.py` | 用桌面壳同一条 token 路登录**本机 117 生产工作台**（127.0.0.1:18799），playwright 录 1920×1080；默认不发送，`--allow-send` 且会话名命中白名单（演示/BOUNDLESS/test）才真发 |
| `record_chatx.py` | 按章节 actions 真录 clip（`click_css_text .ftab 群组` / `click_text BOUNDLESS` / `send_demo …`），记 `ready_offset`（裁掉登录/连接头） |
| `make_episode.py` | 拼装：jingle 开头唱（叠标题层在真录屏上）→ ACE **rap LoRA** 说唱 take → 每 4 行一段切一支真录屏 → 逐字 `\k` 卡拉OK ASS（按 take 的 ASR 分段时间轴）→ 尾卡二维码 |
| `lyrics/E1_rap.txt` | 说唱教学词（kind=rap 词闸放宽到 7~14 字/句） |

**改版规则（指针 + 真聊真发，E1–E12 同一套）**

- **指针/得点**：`pointer_overlay.js` 大号光标 + 蓝框高亮 + 底部说明条；讲到哪指到哪。
- **真聊双会话**（白名单，`--allow-send` 才真发）：
  - `BOUNDLESS` `telegram:8244899900:8506426282` — 与 Jie 英文往来（主画面）
  - `ZH_DEMO` `telegram:6834964252:me` — 「智聊支持」自聊，中文/多语互译演示
- **铺种**：先清工作台旧日志监控垃圾 → `python seed_demo_chats.py --episode all --pin`（E1–E12 功能介绍真发，Jie 会真回）
- **成片**：`out/E1/E1_h426_episode.mp4`（57s）· `out/E2/E2_h428_episode.mp4`（48s）；其余集录屏批量进行中，有 rap take 后 `make_episode.py E* --take <hist>`。

---

# 附：竖屏唱歌式场景片首样（03:03 前的版本，保留产线）

方案：`docs/方案_视频教程与介绍体系_竖屏横屏唱歌式场景_2026-09-16.md`。本目录＝方案 §五 流水线的落地件 + V1 首样。

## 首样结论（老板人耳前的机器闸全过）

| 环节 | 结果 | 证据 |
|---|---|---|
| 歌词词闸 | 13 件全 PASS、0 WARN | `python lyric_gate.py` |
| ACE 原创作曲（hub `/api/song/create`，turbo 27 步） | 45 s 一曲 ≈ 20~35 s GPU；产物 WAV 48k 真音频 | `out/V1_short_s*.wav` + `.json`（history 421/422/423） |
| 15 秒高光 MV（hub `/api/song/mv`，自动选副歌 18 s 起，口型演唱） | mp4 864×1152 双流 ≈ 50 s | `out/V1_h423_mv15s_xiaojie_brand_A.mp4` |
| 9:16 拼装（钩子卡 2 s + MV 15 s + CTA 尾卡 5 s） | 1080×1920/30fps H.264+AAC 22 s；抽帧目检 3 帧 | `out/V1_h423_xiaojie_brand_A_vertical.mp4` + `_t1/_t8/_t19.png` |

**待老板人耳/人眼裁决**：3 个 take（hist 421 / 422 / 423，同词同风格不同 seed）选一把「品牌歌手」声；
小界形象候选 A（女）/ B（男）选一（`avatar/`，均为 AI 合成脸、非真人）。**423 是唯一唱满 verse+chorus+outro 的 take**
（421 在 33 s 唱完、422 前 13 s 是纯前奏）。

## 已知占位（发布前必换）

1. **画中画录屏是官网页面**（复用 20260827 `footage_zh.webm`），不是统一收件箱真录屏——需一个带演示数据的
   工作台实例（或 mock worker）录 15 s 入站→翻译→拟稿→发送。
2. 副歌里「智聊」二字在唱段中辨识度一般（whisper-small 听成「只料/只了」）——可选：品牌词改唱「智聊 ChatX」双语、
   或在字幕上加粗高亮（已做字幕）。人耳定。
3. ASR 逐句命中用的是本地 whisper-small（176:8765 ASR 本机不可达），对唱段偏保守，只做索引不做裁决。

## 横屏教程 T1「三分钟装好智聊」首版（`out/T1/T1_install_zh.mp4`，85 s，1920×1080）

结构＝方案附录 B：标题卡 + jingle 6 s → 要点卡 → **生产站 `/download/chatx` 真录屏**（下载按钮/SHA-256/安装教程入口）→
SmartScreen 示意卡 → 首启向导示意卡 → 主界面导览（真工作台截图，**会话列表已高斯打码**，四处标注）→
**生产站 `/pricing` 真录屏**（6U 新人包）→ 尾卡（下一集 + 二维码 utm `t1-zh`）+ jingle 尾奏。
念白 edge-tts 小晓（中性播音声，营销素材不用人设克隆声），字幕与念白同源、自动折行。

- jingle 取 `out/JINGLE_full_s2144348006.wav`（2 抽里唯一唱出词的那把；425 号 take 命中全 0 已弃）。
- **占位/待补**：安装器、SmartScreen、首启向导三步目前是示意卡——录真装机需一台干净 Windows（装 `ChatX-Setup-1.0.85.exe`
  500 MB）；定价页录屏里 cookie 条仍偶发入镜（accept 按钮点击时机问题，下轮修）。
- 复跑：`python make_t1.py all`（vo → record → cards → assemble）；`SAMPLE_SITE` 可切本地站。

## 文件

| 文件 | 作用 |
|---|---|
| `registry.json` | 选题注册表（V1~V9 公域、G1~G3 私域、JINGLE）+ 4 套风格提示词 |
| `lyrics/*.txt` | ACE 格式歌词（`[verse]/[chorus]/[outro]`，每句 6~9 单位） |
| `lyric_gate.py` | 词闸：格律 / 收益禁词 / iGaming 词表 / 裸数字 / 副歌品牌词 |
| `make_song.py` | hub 作曲（含 176 显存舞步：卸 ollama → park → ensure ace → 出歌 → park ace）→ 验证 → 本地 ASR 命中 → `--mv` 出口型 MV |
| `make_vertical.py` | 9:16 拼装：钩子卡 / MV 全幅 + 录屏画中画 + ASS 歌词（按 take ASR 时间轴）/ CTA 尾卡（二维码 + AI 生成标注）；`audience=private` 自动换隔离域口径 |
| `make_profile.py` | 在 hub 建品牌数字人档（合成脸） |
| `make_t1.py` | 横屏教程 T1 全产线（念白 / 生产站录屏 / 7 张卡片 / 分段成片 + concat + 验收 + 抽帧） |
| `show_takes.py` | 列 take 的 ASR 分段与命中，给人耳当索引 |
| `avatar/` | 小界候选 A/B（AI 合成，非真人） |
| `out/` | 产物（wav/json/lyrics.txt/mv/mp4/抽帧）+ `V1_cards/`（卡片与 ASS） |

## 复跑 / 扩产

```powershell
cd deliverables\video_songs_20260916
python lyric_gate.py                                   # 词闸
python make_song.py V2 --tries 3                       # 3 抽（短版：首 verse+首 chorus+outro，45s）
python make_song.py V2 --mv <history_id> --profile xiaojie_brand_A
python make_vertical.py V2 --take <history_id> --hook "五个软件|一个收件箱"
```

- 全词长版：`--cut full --duration 100`（横屏 F3/合集用）。
- 私域 G 系列：registry 已标 `audience=private`，尾卡/水印自动不写 bd2026.cc；**不进 /videos、不发大陆平台**。
- 纪律：任何产物必过 magic bytes + ffprobe；首样过人耳前不批量（本轮只做了 V1）；
  hub 直播让路 409 即停不 force；ACE 任务后必 park（脚本已内建）。

## 2026-09-16 实录

- 01:50 V1 首抽（全词 18 行 / 45 s）：ACE 把 verse 前两句唱两遍、verse2/chorus2 全丢 → 定短版规则（`--cut short`）。
- 01:57 短版 3 抽全部真 WAV；ACE 加载 ~10 s + 生成 ~5~20 s；VRAM 12 GB 空闲即可跑（ollama qwen3-vl 会被 ChatX 重新拉起，不影响）。
- 02:03 MV 首次 503「口型服务不可达」→ 脚本加 ensure 当前口型引擎（musetalk→`lipsync`），二次 190 s 出片（含引擎冷启），三次 55 s。
- 02:20 建合成脸档 `xiaojie_brand_A` 出 MV → 拼装成片；真人脸（hub 档 001）验链产物已删除，不入交付。
- 02:27 JINGLE 30 s 2 抽（424 可用 / 425 无词）；02:35~02:50 T1 首版：drawbox 的 `h` 是盒高不是画高（首版字幕条画到顶部）、
  drawtext 不折行（加 `_wrap`）、cookie 条延迟出现——三处修完出片 85.2 s 双流验收过。
