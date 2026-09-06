# L 批进度总账（跨对话 / 跨账号交接的唯一入口）

> 任何新对话、新账号、崩掉后重开，**第一件事就是读这里**。不依赖聊天记录。
> **谁改这里：** 每条指令的执行方在**收工时（或额度将尽时）**更新自己那一行，**只改自己那一行**。
> 建立于 2026-09-06 08:1x。公共底座 `docs/修复指令_L批_并行_2026-09-06.md`（去向表 / 文件归属 / 决策 D-L1–D-L8）；分析 `docs/未修复问题总盘_三视角_2026-09-06.md`。

## 一句话现状

台账 213 单：未关闭 69 = **49 张代码已进 1.0.74 待验**（K-5 ④⑤ 未走）+ **20 张真未修 #194–#213**（09-05 漏立单 28 份报告 + 09-06 复测新发现）。两条系统性缺口：① 内测机走干净包，「出厂开」种子没到（D1 自动推进 / D7 危机留痕在两机仍关）；② 桌面包带内网 GPU/STT 默认地址 → 熔断回落是多项质量问题共同上游。
**L-6 服务端两件 + L-5 先开**（当天见效），L-1…L-4 并行，L-7 在 L-5 后。目标版本 1.0.75。

## 进度表

| 指令 | 主题 | 工单 | 预算 | 状态 | 已完成 | 剩余 | 落点表 | 已花 |
|---|---|---|---|---|---|---|---|---|
| **L-1** | 出站可信度（拆条止血 / 问候不编事实 / 无响应文案） | #210 #208 #194 | $120 | **收工（09-06 10:1x）**；.py 待重启装载，模板/JS/i18n 已热更 | A `2f1cca8b`（仅显式换行才拆 + 短回复门 80 + 逐句存量迁移 + 出口折叠 + 设置页风险提示/审计）+ `630835d7`（**出厂关**：bubbles 由 feature_registry A 类降 B、两份桌面种子 `enabled:false`——这就是 skuio 机 enabled 被打开的来源）/ B `ecf2a5e2`（逐条镜像不变量 + 响应 `message_ids` + 汇总日志 + 前端「已拆 N 条」；前端两 hunk 随 L-3 `19d60567` 入库）/ D `0448aebf`（无响应只留附件条一处，按真相分「后台重启中」/「没拿到发送结果」）/ C **代码在 L-3 `a781e36b`**（暂存 30 秒内被兄弟线整 index 提交带走）+ 空标记 `556d1aec`：近况编造守卫 + 问候 prompt 硬约束 + 历史随口一说不延续 + 请求语不判 greeting；相关门禁 237/7/326/14/109 passed，邻接 210 passed | 装载后真机核：skuio 首启日志「拆条已按 1.0.75 默认收紧」+ Sceya 会话单条；`[fabrication_guard] 无来源近况陈述` 与 `整条回复都是无来源近况陈述` 两行的规模（成规模则做「剥空→重生成」）；#194 可当天核。验收口径交 L-7 见落点表 | `发版对账_v1.0.75_L1.md` | ≈$80 |
| **L-2** | 人设与语音闸门 | #205 #203 #204 #202 #206 #192残 | $180 | **代码全进树，待装载** | B `a3216231` 兜底不换声 / A `3f31eeab` 语音三态 + 校验 + 新建默认 off / C `a1e54876` JSON 导入用户版不渲染 + 预览确认撤销 / D `bda3bbce` 胶囊四源同口径 / E `d0593c7b` 人设备份与迁移三入口 / F `df2f5c2a` 补齐向量人话 + 页头按钮 title | 装载本条 `.py`（L-7）；繁体 regen；#192 残要 skuio 放大图确认「7 ▾」＝窄窗头像按钮后再换图标 | `发版对账_v1.0.75_L2.md` | ≈$135 |
| **L-3** | 工作台状态真相 | #207 #170 #196 #209 | $150 | **收工（09-06 09:5x）**；.py 八处待重启装载 | A `b3a34895` / B `19d60567` / C **`a924426f`**（⚠ 卷入 L-5 B1 提交，共享 index 竞态）/ D `a781e36b`（⚠ 反向卷入 L-1 暂存 5 文件，已验绿）；42 新例 + 相关既有全绿；浮回探针绿 ⇒ 被埋 7→8 是人工归档非缺陷 | 装载后真机验收（口径见落点表）；D 项暗/亮截图待 skuio 机；交 L-6 B（infra 告警真解）、L-1/L-5（提交归属说明）、L-7（#170 #173 可随 1.0.75 标 fixed） | `发版对账_v1.0.75_L3.md` | ≈$105 |
| **L-4** | 后台可见性分层 | #197 #198 #212 #200 #201 #199 #211 | $150 | **收工（09-06 1x:xx）**；.py 十余处待重启装载，模板 / i18n 已热更（`ui_client_hide` 缺席按不藏，中间态自洽） | A `395acee7`（CLIENT_HIDDEN 加 help/strategies/singing/voice_eval + PARTNER_ONLY 三条 settings 深链 + flavor 三层 + 开发者模式 + 侧栏「研发/代理商」角标）/ F `b5d146bd`（#201：噪音不入队 + 私事→客户记忆不进 KB + source=learner + 通过已选 ≤20 须确认 + BM25 不再假 99% + 译文 + 存量一次性清标）/ B `5512c0a6`（#197：三卡用户版隐藏 + 徽标与授权卡同源读 plan_source + 演示仅空工作区 409 + 功能总览人话）/ G `a4f453da`（#199 #211：`_client_tier_upgrading.html` 横幅 + 操作面收起 + 唱歌能力位资产门禁 409）/ C `f9edb52b`（#198：整页收起 + 「机器人斜杠命令（旧）」标范围 + 培训入口存在性 + `_page_error.html` 人话 404）/ D `e180d1b1`（#212：整页收起）/ E `d6942318`（#200：语音单一下拉 + 引擎状态 / 缓冲语并入人审默认关 / 删人设 ID 秒数表（既有覆写保留）/ 平台表折叠 + 实际值灰字 + LINE 打字硬禁）；新 87 例全绿，邻接门禁全绿。**开发者模式 flag（L-2 读）**：session 键 `developer_mode`（仅 `dev_unlocked` 同真时生效；`/developer` 页「开发者模式」卡开关 → `POST /api/developer/developer-mode`；退出登录 / `/developer/logout` 即关，不落 overlay）。模板全局：`ui_flavor`（client/partner/internal）、`ui_developer_mode`（bool）、**`ui_client_hide`**（bool＝client 形态且未开开发者模式→该藏的都藏；L-2 三处「开发者模式可见」写 `{% if not ui_client_hide %}`）。纯函数：`src.web.ui_visibility.client_hide_active(cfg, developer_mode)`；nav：`get_nav_context(cfg, developer_mode=)`。 | 装载后真机验收（口径见落点表「交 L-7」）；**待老板 4 条**（D 三档预设 / override 徽标 / partner 是否藏 bug_tickets / 歌房资产不齐运行时视为关）；**交 L-2**：语音总闸运行时硬门禁 + 人设工作室「快/中/慢」；**交 L-5**：代理商机 `flavor: partner` 怎么落；P3：`kb_miss_log` 客户维度 / 缓冲语人设口吻；既有失败 `test_workspace_sidebar` 5 例（HEAD 同样红）留 L-7 | `发版对账_v1.0.75_L4.md` | ≈$120 |
| **L-5** | 内测渠道与出厂默认 | #166残 #185残 | $100 | **已完成**（09-06 09:5x） | A 三键升 feature_registry **A 类基线**（真正能到存量 clean 升级机的载体）+ desktop.min 种子 + example 同值 true `64f40009` / C 种子缺席 WARNING（桌面态）`d5d30095` / **B 选 B1**：渠道烘进包（smart=`npm run dist:win` → `dist/latest-internal.yml`，app-update.yml 指 `downloads/internal/`；clean 不变）+ `publish_chatx.ps1 -Channel internal` + fleet `edition` 列 `a924426f` `85612834`；内测渠道已上架 1.0.74 smart（VPS+R2），**yunsheng 已切 `smart/internal` 并实锤壳拉内测 feed**（nginx 09:24:38） | D 交 L-7：1.0.75 两包分发 `publish -Channel internal`；kouxing/lianbei 推 `dist\` smart 即切；两台内测机**手装一次** `dl/downloads/internal/ChatX-Setup-1.0.75.exe` 切渠道（无 SSH）；验收清单+回访文案见落点表 §5。`duty_evidence probe` 标 clean 未做（信标只传 ERROR 级）→ 方案定死交 L-7（落点表 §5.4 diag 元数据三处小改）。老板拍板后追加：**lite 独立渠道已做**（`-Channel lite`，未实弹打包）。⚠ `a924426f` 误含 L-3 已暂存 8 文件（内容已在 HEAD，L-3 无需重提，见落点表 §6） | `发版对账_v1.0.75_L5.md` | ≈$70 |
| **L-6** | 通道与服务端 | #213 #195 内网外泄 secret_key | $120 | **收工（09-06 11:5x）**；A 服务端 + 网关**已生效**，B/C/D `.py` 待装载，模板 / i18n 已热更 | A `3cca7d84`：176/140 两台中继 `qwen3-vl:8b-instruct` **就地** `PARAMETER num_ctx 8192`（原 4k 版留 `-orig4k` 回滚点；实测 Ollama `/v1` 忽略 `options.num_ctx`，Modelfile 是唯一生效点；「新 tag + 网关指向」在 140 会与 LAN 直连的 4k 版互挤显存故弃）+ 网关 `vision_ctx_overflow` 识别 + 管理员 TG 告警（30 分钟节流）+ website 10:51 已部署；「智谱令牌过期」实为设备令牌 `cx.…` 被误当智谱 key 送出去——`_zhipu_credentials` 修掉，识图失败三档人话（未配置 / 忙 / 无文字）替代「识别翻译不可用」；4753 tokens 等价复现两机 200 / B `7c3e6e45`：新 `utils/desktop_mode.is_desktop_client` 单一闸，STT 140:7854 / 改写 173 / 看门狗 LAN GPU 巡检桌面态不再指向内网，`apply_hosted_asr` 网关接管时摘内网备级，语音失败人话，`embedding_status()` 三态口 / D `e56b72a0`（⚠ 兄弟线代提，内容一致）：桌面首启随机 `secret_key` 写 `config.local.yaml`，`[SECURITY]` 改绑不再触发，开发者页密钥行显真实状态 / C `2d804c5f`：贴纸 `.tgs/.webm/.webp` 三态落盘 + `<stem>.thumb.webp` 缩略图 + 工作台三态渲染 + 识图走缩略图；新测 65 例 + 邻接 ≈1050 例过；⚠ `d05c9fe2 fix(line)` 曾带走本线错位暂存的 `errors.py` 8 行（HEAD 一度 SyntaxError），`3cca7d84` 已修正 | 装载后真机核：#213 钧机今天即可验（服务端已生效）；#195 请钧**再收一次**动图 + 视频贴纸；skuio 机 3h 无 LAN GPU toast；两机首启无 `[SECURITY]`。交 L-3 C（infra toast 收窄）/ L-4 F（`embedding_status`）/ L-2（`test_avatar_voice` 两红是 `a3216231` 契约变更未更新用例）/ 运维（machines.json 说 198 是识图单点、隧道实通 176/140，口径对不上） | `发版对账_v1.0.75_L6.md` | ≈$95 |
| **L-7** | 值守登记链 + K-5 收口 | 流程 + 49 单 + 61 单回访 | $100 | 未开工 | | A 报告自动立单 / B 每日对账 / C smalltalk + 序号哨兵 / D K-5 ④⑤ | `发版对账_v1.0.75_L7.md` | |

## 老板拍板记录

（默认值见底座「老板决策」表；老板改口在这里追加一行：日期 / 决策号 / 改为）

- 2026-09-06 09:5x / 新规 D-L9 **提交渠道**：内测人的 bug 一律走本机 Cursor 报告（真实证据源），**不要提交到群里**；群只答值守追问。已落地：`tools/duty_channel_reminder.py` 随 DutyWatchTick 每 2 分钟跑——skuio 群提交自动提醒（6h 一次）+ 新报告自动回执「收到 <码>」；**不自动立单**。影响 L-7：A「报告到达自动立单/挂单」从应做变**必做且优先**（群不再是 skuio 的立单入口），回执已存在不要重复发；D 回访文案带上这条规则。

- 2026-09-06 09:4x / D-L1 补充（L-5 提出的待拍板项） / 老板：「需要拍板的按你的建议去做」→ **lite 定制档也建独立更新渠道**（`latest-lite.yml` / `downloads/lite/`，与 internal 同法烘进包），已做；「版本旁标 clean/smart」按 L-5 落点表 §5.4 的 diag 元数据方案交 L-7 直接执行。

## 全批收口（L-7 或最后收工的对话做）

- [ ] 七条落点表齐 → 合并 `docs/发版对账_v1.0.75.md`（照 1.0.74 骨架：单号 → commit → 验收口径）
- [ ] `restart_preflight` → zhiliao 装载 → 全量回归 + gate_sweep
- [ ] 1.0.75 打包（**smart + clean 两包**，L-5 定内测机装哪包）→ 坐席三台 → publish → 两机装载核版本
- [ ] 台账：新单标 fixed（只经端点 / `duty_ledger_fix`）；回访两份汇总 + 逐单验收口径
- [ ] `docs/值守症状族对账单.md` 更新；本总账每行填「已花」

## 开对话话术（每条一段，整块复制）

见各指令文件末尾；汇总在总盘 §4 之后由老板在聊天里拿到。
