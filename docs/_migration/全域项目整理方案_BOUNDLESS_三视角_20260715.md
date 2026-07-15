# 无界科技 BOUNDLESS · 全域项目整理方案（三视角 · 单仓 wujie）

> 用途：把分散在多台机器/多个仓库的自研资产 + 官网 + 品牌 + 文档，统一整理进 **117 电脑**上的**一个新 git 单仓 `wujie`**。
> 目标：每个产品有独立拼音文件夹、功能清晰独立，又通过共享底座互相关联，形成 **获客 → 承接 → 分身 → 交付 → 复购** 的工作闭环。
> 依据：本机（DESKTOP-SH6IM7V）的引擎源码 + 经 SSH 实勘 117（`D:\workspace`、`D:\faceX\mfys`）真实目录/git/代码量 + 117 上团队已有的《工作区分析》《实施01–08》等规划文档。
> 版本：V2（V1 基于不完整假设，已按 117 实况全面修订）。

---

## 0. 一句话结论

**在 117 建单仓 `wujie`：`engines/` 放 3 套自研核心引擎（一次维护、多产品共享）+ `products/` 放 7 条产品线（各用拼音独立文件夹，做产品化封装）+ `platform/` 共享底座 + `website/` 官网 + `brand-assets/` + `docs/`。** 用"产品拼音文件夹"实现独立，用"共享引擎/底座"实现关联与闭环。

> 关键澄清：现有 3 套自研代码库**每套横跨多个产品**（1 个引擎 = 4 个产品），所以不能"一个产品塞一个独立代码库"。正确做法是**引擎共享一份、产品文件夹做薄封装**（这正是团队已在做的：LingoX = telegram 引擎 + `lingox.overlay.yaml` 裁剪）。

---

## 1. 真实资产盘点（SSH 实勘 117 + 本机）

| 资产 | 位置 | 真实身份 | 代码量 / git remote | 归属 |
|---|---|---|---|---|
| **模仿音色 / mfys** | 本机 `D:\projects\模仿音色`（新）· 117 `D:\faceX\mfys`（旧, 6-24 克隆） | 数字分身引擎：换脸/克隆/数字人/同传 | 本机 ~256 根 .py；117 克隆 489 .py；remote victor2025PH | 自研核心 |
| **telegram-mtproto-ai** | 117 `D:\workspace` | 承接引擎：客服/统一收件箱/AI回复/翻译栈/陪伴 | py=1530, ts/tsx=3528；remote yunkeji2026；**119 未提交** | 自研核心 |
| **mobile-auto0423** | 117 `D:\workspace` | 获客引擎：真机 RPA（FB/Messenger/TikTok） | py=629；remote victor2025PH；branch feat-a-p2x | 自研核心 |
| **index-tts** | 117 `D:\workspace` | 第三方 TTS（IndexTTS2），VoiceX 后端依赖 | 7GB；remote index-tts/index-tts | 第三方（vendor） |
| **_server-yuntech / web117** | 117 `D:\workspace` · 本机 `C:\web117` | 官网 usdt2026.cc（Next.js 统一销售门面） | ts/tsx=1985；deploy.ps1(Posh-SSH) | 自研·官网 |
| **brand-assets** | 117 `D:\workspace` | 品牌资产库（logo/图标/排版/头像/背景/字体） | 78MB；**7 产品图标齐全** | 共享资产 |
| **docs-business** | 117 `D:\workspace` | 商业/战略/实施文档（含《实施01–08》三视角报告） | 0.1MB | 文档 |

> 结论：不是"6 个平级项目"，而是 **3 套自研引擎 + 官网 + 品牌库 + 文档**；引擎横跨产品，官网是统一门面。
> **权威副本**：引擎以**本机较新副本**为准（117 的 `D:\faceX\mfys` 是 6-24 旧克隆，迁移时以本机覆盖）。

---

## 2. 产品体系（团队已定「三系」· 官方口径）

来源：117《实施07_三系产品命名与矩阵落地》。母品牌 **无界 BOUNDLESS** 统领，三系各破一"界"：

| 产品系 | 产品（中文·英文·拼音） | 承载引擎 | 合规可见性 |
|---|---|---|---|
| **智连系 Growth**（破沟通与成交之界） | 智拓 ReachX · `zhituo` | huoke (mobile-auto0423) | 🔴 隔离/准入（RPA 触 ToS） |
| | 智聊 ChatX · `zhiliao` | chengjie (telegram) | 🟠 主站(企业API版) / 准入(个人号版) |
| **幻境系 Studio**（破容貌/声音/身份之界） | 幻颜 FaceX · `huanyan` | avatarhub | 🔴 隔离/准入（深伪监管） |
| | 幻声 VoiceX · `huansheng` | avatarhub + vendor/index-tts | 🟢 主站（授权音色声明） |
| | 幻影 LiveX · `huanying` | avatarhub | 🟢 数字人主站 / 🔴 直播换脸准入 |
| **通达系 Lingo**（破语言之界） | 通译 LingoX · `tongyi` | chengjie (翻译栈 overlay) | 🟢 主站·**主推现金流** |
| | 通传 VoxX · `tongchuan` | avatarhub (同传) | 🟢 主站 |
| （母品牌底座） | 无界底座 · `dizuo`（私有 LLM 接入层） | platform (llm_backends) | 🔴 "无审查"话术须移出公开货架 |

> 合规红线（来自《实施02A/03》）：**「无审查 / 无禁区 / USDT / 换脸」是支付/尽调/投放的红灯词**。整理时必须支持"合规主站 + 隔离准入区"双层陈列（母品牌统一背书，支付主体/域名/可被索引页面分开）。

---

## 3. 目标目录结构（117 · 新单仓 `wujie`）

```
wujie/                         ← 新 git 单仓（无界科技全域）
├─ README.md                   全域总览 + 产品导航 + 一键起停
├─ platform/                   ★共享底座（闭环中枢，产品/引擎都依赖，绝不反向依赖）
│  ├─ brand/                   设计令牌(brand.ts/brand.css) + 指向 brand-assets
│  ├─ identity/                身份·资产总线（Profile：克隆声音+形象，一次投入跨产品复用）
│  ├─ licensing/               授权/计量/SKU 门控（billing + gate + license）
│  ├─ compliance/              溯源(C2PA/水印) + consent/GDPR + 可见性分层(public/gated/custom)
│  └─ observability/           KPI 埋点 / 遥测 / 漏斗看板（打通 CAC/CPL/ROAS）
├─ engines/                    ★3 套自研核心引擎（多产品共享，保留可识别名）
│  ├─ avatarhub/               = 模仿音色/mfys（换脸·克隆·数字人·同传）★本机新副本为准
│  ├─ chengjie/                = telegram-mtproto-ai（承接·收件箱·翻译栈·陪伴）
│  └─ huoke/                   = mobile-auto0423（真机 RPA 获客）
├─ products/                   ★7 条产品线：拼音独立文件夹（薄封装：SKU/overlay/落地页/交付包/文档）
│  ├─ zhituo/     智拓 ReachX   → engines/huoke
│  ├─ zhiliao/    智聊 ChatX    → engines/chengjie
│  ├─ tongyi/     通译 LingoX   → engines/chengjie（翻译 overlay，主推）
│  ├─ tongchuan/  通传 VoxX     → engines/avatarhub（同传）
│  ├─ huansheng/  幻声 VoiceX   → engines/avatarhub + vendor/index-tts
│  ├─ huanying/   幻影 LiveX    → engines/avatarhub
│  └─ huanyan/    幻颜 FaceX    → engines/avatarhub
├─ website/                    官网 usdt2026（Next.js）← _server-yuntech / web117
├─ brand-assets/               品牌资产库 ← brand-assets（7 产品图标齐全）
├─ docs/                       商业/战略/实施/运维文档 ← docs-business + 各仓 docs
├─ vendor/                     第三方引擎（index-tts 等；git 忽略，按需部署）
├─ deploy/ tools/ packaging/   多机拓扑/provision/门禁(claims_lint,gate)/打包
├─ models/ data/ logs/ dist/   运行时/产物（git 忽略）
└─ .gitignore
```

依赖规则：`website / products` → `engines` → `platform`；产品之间**横向零 import**，只经 platform 的身份/授权/注册表/HTTP 契约交互。

---

## 4. 工作闭环设计

### 4.1 业务闭环（官网门面串起全线）

```
官网 usdt2026(统一门面) ─► 智拓 ReachX 真机加友/引流 ─► 智聊 ChatX 统一收件箱·AI承接·翻译·转人工
      ▲                                                             │
      │                                                             ▼
   复购/升档 ◄─ 授权计量交付(platform/licensing) ◄─ 分身/跨语内容(幻声·幻颜·幻影·通译·通传) ◄─┘
```

- 获客(智连系) → 承接(智连系) → 用分身/跨语能力(幻境系+通达系)成交内容 → 授权计量交付 → 复购。
- 官网是唯一销售门面；`platform/licensing` 的计量+SKU 门控是收款闭环节点。

### 4.2 技术闭环（共享底座让 7 产品咬合）

**身份/资产总线是杀手级连接点**：在 VoiceX 克隆一次声音、FaceX 定一次形象 → 同一 Profile 驱动 LiveX 直播、VoxX 同传、并作 ChatX 人设音色。一次投入、全线复用 —— 这就是"独立又关联"的落地。底座另统一授权门控、合规出口、KPI 埋点。

---

## 5. 三视角分析（融合 117 已有《工作区分析》《竞品对标》实况）

### 5.1 开发工程师（可维护性 / 技术债）
- **实况痛点**：巨石文件（telegram `facebook.py` 1.3 万行、`web_i18n.py` 1.5 万行；引擎 `avatar_hub.py` 全产品路由耦合）；仓库卫生差（本机根目录 318 个 `_*` 临时脚本，telegram 40+ 调试脚本）；依赖不锁版本；telegram 曾 3 份副本、当前 119 未提交；大文件进树（本机 `_pending_models`132G/`dist`64G、index-tts 7G）。
- **整理后**：3 引擎各一份、products 薄封装；`platform` 统一底座；大文件出 git（vendor/provision 重建）；单仓统一 `.gitignore`+pre-commit+门禁 CI；`app_config.SERVICES` 续做服务单一真相。
- **收益**：改一个产品不动其它；新机 `provision` 可复现；按产品独立发布/定价。

### 5.2 市场经理（变现 / 转化 / 合规）
- **实况痛点**：官网"五产品线大杂烩"、通译/智聊套餐张冠李戴、最该卖的通译无定价、最高危的"无审查换脸+USDT"摆头条；KPI 全 TODO；卖点分散无统一叙事。
- **整理后**：7 产品各独立拼音文件夹 = 独立 SKU/落地页/交付包；官网按三系陈列 + `capability_matrix` 能力门禁对齐；**合规主站(通译/智聊企业版/幻声/数字人/同传) + 隔离准入区(换脸/RPA/陪伴/无审查)** 双层物理分离；通译提为首屏主推现金流。
- **收益**：SKU 目录清晰、可举证营销、保护低风险业务不被高风险连累、升档路径明确。

### 5.3 用户 / 运营（体验 / 信任 / 安全）
- **实况痛点**：密钥裸奔（服务器密码.txt / config.yaml Key / creds.json）、合规≈0（无 consent/GDPR/DSAR）、默认弱口令、分不清买的是哪个产品、安装摩擦、克隆"试了才知道"。
- **整理后**：机密统一 `secrets/`+轮换+`.gitignore`；`platform/compliance` 落最小合规层；官网一门面按三系分区；一个身份跨产品复用；装前硬件体检、免费试听带水印、自助下单自动交付。
- **收益**：旅程连贯、信任可见、摩擦递减、账号/后台不再裸奔。

---

## 6. 迁移执行计划（117 · 分阶段，可运行可回退）

### Phase 0 — 决策与安全前置（动手前必做）
- 你拍板第 8 节 4 项决策（git 历史保留方式 / 官网是否同仓 / 合规双层与域名 / 引擎权威副本）。
- **安全前置**：117 上 telegram 119 处、mfys 10 处、index-tts 8 处**未提交改动先提交或 stash**（否则迁移会丢代码）；先做一次密钥普查（`服务器密码.txt`/`config.yaml`/`creds.json` 等移出并轮换）。

### Phase 1 — 新仓骨架 + 仓库卫生（低风险，先做）
- 117 `D:\workspace\wujie`（或独立盘）`git init`，建 `platform/ engines/ products/ website/ brand-assets/ docs/ vendor/ deploy/ tools/`。
- `.gitignore` = 只入源码：排除 `vendor/ models/ data/ logs/ dist/`、`*.pth/*.onnx/*.exe/*.zip`、`node_modules/`、本机根 `_*.*`、机密。
- index-tts 归 `vendor/`（git 忽略，按需 provision）；**不拷** 本机 132G/64G 大文件。

### Phase 2 — 引擎/产品搬迁（中风险，增量，每步可跑）
- 3 引擎迁入 `engines/`（avatarhub 用本机新副本）；官网迁 `website/`；brand-assets、docs 迁入。
- 建 7 个 `products/<拼音>/` 薄封装（SKU/overlay/落地页引用/交付包/文档），复用团队已有 `lingox.overlay.yaml` 模式。
- 抽公共到 `platform/`（brand 令牌、licensing/billing/gate、compliance、observability）。
- 每步跑各引擎自检/回归（引擎 `doctor.py`/`run_all_tests.py`、telegram pytest、门禁 `claims_lint`）。

### Phase 3 — 接口与闭环
- 固化身份/资产总线、授权计量门控、合规出口、KPI 埋点。
- 打通 官网 ↔ SKU/定价 ↔ licensing ↔ 交付；官网按三系 + 合规可见性生成导航/sitemap。

### Phase 4 — 加固上架
- 每产品 README+SKU+演示；CI（测试+门禁+UI 回归）；填真实 KPI；docs 归类。

---

## 7. 新 git 单仓策略（已按你的拍板定稿）

| 项 | 决定（2026-07-15 拍板） |
|---|---|
| 历史合并 | **拉净码重开**：各引擎取当前工作树净码入 `engines/`，不并入源仓旧提交（更简单；旧仓 GitHub 仍在，历史可追溯） |
| 第三方 index-tts | **不入库**，`vendor/` + git 忽略，靠 provision/junction 部署 |
| 官网 web117 | **入同仓 `website/`**（统一门面，与产品定价/能力矩阵联动） |
| 合规分层 | **落地双层**：合规主站 + 隔离准入区（换脸/RPA/陪伴/无审查）用独立域名+主体 |
| 大文件 | 一律不入库，provision/MANIFEST 重建；必要小媒体才用 git-LFS |
| 机密 | 只迁 `*.example` 模板；真实 `.env*`/`config/*.yaml`(含密)/`*.key`/`prod.env.local` 永不入库 + 轮换；`tmp_*` 调试转储不迁 |
| 未提交改动 | ✅ 已在 117 `telegram`(119) / `mobile`(1) 建可恢复快照 `refs/wujie-backup/20260715`（未动工作树） |

---

## 8. 需你拍板 + 我能立刻做的

**需你拍板（阻塞不可逆步骤）**：
1. **git 历史**：`subtree` 保留历史合并，还是拉净码重开（我建议 subtree 保历史）。
2. **官网**：与产品同仓 `website/`（建议），还是保持独立发布仓用 submodule 引用。
3. **合规双层**：合规主站 + 隔离准入区是否落地、隔离区是否独立域名/主体（团队文档强烈建议做）。
4. **引擎权威副本**：确认 avatarhub 以**本机新副本**为准覆盖 117 旧 `mfys`。

**我可立刻做（安全/可回退，无需等决策）**：
- 在 117 建 `wujie` 骨架 + `.gitignore` + README + 把本方案与 117《实施01–08》归入 `docs/`。
- 跑一遍 117 全量资产清单 + 机密普查报告（只读，不动源）。
- 先把各源仓未提交改动落盘/stash（防丢）。

> 确认第 8 节后，我从 Phase 1 落地；不可逆的历史合并与大范围搬迁在你拍板 git 策略后再执行。

---

## 9. 落地记录（Phase 1–2 · 2026-07-15 已执行于 117）

**Phase 1 — 骨架 + 卫生**
- 安全网：`telegram-mtproto-ai`(119 改) / `mobile-auto0423`(1 改) 未提交改动已建可恢复 ref `refs/wujie-backup/20260715`；源仓工作树未动。
- 新仓：`D:\workspace\wujie` `git init`(main) + `.gitignore`(机密/大文件/vendor) + README + 目录骨架，首提交完成。

**Phase 2 — 搬迁 + 机密清洗 + 提交（commit `708c4a3`）**
- `engines/avatarhub` ← 本机较新 `模仿音色` 净码（git-clean，1262 文件，中文名 round-trip OK）。
- `engines/chengjie` ← telegram-mtproto-ai 净码（2174 文件）；`engines/huoke` ← mobile-auto0423 净码（849 文件）。
- `website` ← _server-yuntech（排除 node_modules/.next/.env*）；`brand-assets`（7 产品图标）；`docs` ← docs-business + 本方案。
- `products/<拼音>` 7 个产品薄封装 README（zhituo/zhiliao/tongyi/tongchuan/huansheng/huanying/huanyan）。
- 机密清洗：删除 `users.json`×2 / `debug.keystore` / `yuntech-src.tgz` / 旧子项目 `config.yaml`；正则脱敏 7 文件里的真实 GLM/OpenAI/Bot 密钥为 `${VAR}` 占位；`.gitignore` 追加 per-engine 守卫；提交前门禁扫描（有机密特征则拒绝入库）。
- 结果：4564 文件入库，工作树干净，`.git` 146MB（含 brand-assets 93MB 图片，后续可转 LFS）。
- index-tts(7GB) 未入库 → `vendor/`，靠 provision 部署。

**⚠️ 待你处理（安全）**：`mobile-auto0423/config/chat.yaml` 里的真实智谱 GLM 密钥 `ac5f80…YznB` 在源仓（且源仓有 GitHub remote，可能已推送）——wujie 副本已脱敏，但**源仓那把 key 视为已泄露，请尽快在智谱后台吊销/轮换**。同理排查各源仓 `.env`/`config` 历史提交里的真实密钥。

## 10. Phase 3 落地记录（2026-07-15 · 官网三系 + 合规清洗）

> 均在 wujie 单仓 `website/`，用 `_server-yuntech` 的 node_modules 做 junction 跑 `tsc --noEmit` 真校验（全程 0 错）。

- **key 脱敏提交** `mobile-auto0423@04bc413`：`config/chat.yaml` + 2 个 tiktok 源里已吊销的 GLM key → `${GLM_API_KEY}`（不含你在改的 chat_messages.yaml）。
- **三系 taxonomy** `wujie@a9bc871`：`brand.ts` 7 产品（新增 **通传 VoxX**）+ `category` + `CATEGORIES`(智连/幻境/通达) + `productsInCategory` + `PRODUCT_ORDER` 按系重排；`productMeta/layout(schema)/routing` 的所有 `Record<ProductKey>` 补 voxx；`ProductMatrix` 改按三系分组陈列（底座横幅）；概述文案六线→三系。
- **content 合规清洗** `wujie@f8854d9` + `27f33ea`：
  - **通译/通传拆分**：`translate` 卡重挂 **通译 LingoX = 聊天翻译 SCRM**；新增 `interpret` 卡 = **通传 VoxX 语音同传**；定价均置「按需报价/Quote」占位（数字待你回填）。
  - **USDT 双层（dual）**：合规主站 hero/trustline/pricingSection/SEO schema 去掉「USDT 结算」头条卖点、计价单位改 USD；USDT 作为结算方式**保留**在下单步骤/FAQ/联系页（真实收款不变）。
  - 违禁词（无审查/无禁区/uncensored）全站复扫 = 0。
- **状态**：`tsc --noEmit` 全绿；voxx 图标暂复用 lingox（待 `build-boundless-marks` 生成 voxx.png）。

**待你回填 / 决定**：① LingoX/VoxX 的实际价格数字；② voxx 专属图标；③ 部署前在 `website/` 跑一次 `npm run build` 自验；④ **wujie 从此为唯一真源**——请从 `wujie/website` 部署，旧 `C:\web117`(5 产品)/`_server-yuntech` 归档停用。

**Phase 3 剩余（待续）**：`platform/` 共享层抽取（身份·资产总线 / 授权计量 / 合规出口 / KPI）；官网 Navbar 三系下拉；官网↔SKU↔license↔交付 业务闭环打通。

---

## 11. Phase 4 实施记录 + 实施中优化（2026-07-15）

> 按审计 §5 P0/P1 开工；实施中边做边深挖，发现更优解就改（下附）。均已提交、`tools/repo_doctor.ps1` 复验。

**已落地（提交）**
- **Navbar 三系下拉** `dfa2a5e`（桌面+移动，tsc 0 错）。
- **platform 契约层** `c7803fe`：`platform/README.md` 定义五契约 + 当前实现所在 + 迁移顺序。
- **P0 仓库减重** `b9a17c6`：`git rm --cached` 掉 **陈旧重复站 `engines/chengjie/website`(232)** + **Playwright 浏览器缓存 `avatarhub/demo_record/.pwprofile`(237)** = 少 **469** 跟踪文件（磁盘不删、可回退），补 `.gitignore`。
- **tools 全域门禁** `b2233d1`：`tools/repo_doctor.ps1`（把审计 §4 变成可复跑门禁，输出 FAIL/WARN/GREEN）+ `tools/prepush_cleanup.ps1`（LFS+历史清理，push 前一次做）+ `.gitattributes`（字体/媒体 LFS 策略）。
- **P1 products 范式** `5643ce4`：`products/tongyi/product.yaml`（SKU/承载引擎/裁剪 overlay/官网落地页/合规可见性/交付计量 单一真相），作为其余 6 产品模板。
- **门禁结果**：`repo_doctor` = **FAIL 0 / WARN 2**（字体>10MB 待 LFS、platform 待抽实现），tracked 4566→4101，worktree 干净。

**实施中的 5 处优化（在原方案上再优化）**
1. **减重只动"无歧义垃圾"**：原审计建议连 `clothes/`、字体删除一起做；深想后**只 untrack 重复站+浏览器缓存**，`clothes` 可能是真需素材、字体应走 LFS 而非删——避免误伤。
2. **LFS+历史清理挪到"push 前一次做"**：中途 `git lfs migrate`/`filter-repo` 会反复重写历史且磁盘收益要 push+prune 后才兑现；故封装成 `prepush_cleanup.ps1`，只在接新 remote 前跑一次。
3. **审计清单→可执行门禁**：`repo_doctor.ps1` 把静态检查变成一条命令的红/黄/绿闸门，并把 WARN 定义为"完善度待办清单"，比一纸文档更能持续用。
4. **products = 清单模式（引用而非复制）**：`product.yaml` 只登记"用哪个引擎+哪个 overlay+哪个落地页"，不拷贝引擎代码；实施中**发现并修正**一处坏引用（overlay 校验脚本原在 gitignore 的 scratch 未迁入，已在清单标注待提升）。
5. **platform 坚持"契约先行、不搬代码"**：再次确认——跨 env/跨机的 Python 抽取在本地无法验证、风险高；platform 落地必须**在装有各引擎 conda env 的机器上、借该引擎自带测试**逐个迁移（compliance→brand→observability→licensing→identity）。

**下一阶段（Phase 5）实施与改进**
- **P0 platform/compliance 抽取**（须在装 avatarhub env 的机器上）：把 `provenance/watermark` 收敛为 `platform/compliance`，avatarhub 留 1 行 re-export 兼容 → 跑 `doctor.py` 回归 → 绿了再删旧。
- **P1 products 补齐其余 6 个** `product.yaml`（照 tongyi 模板）+ 回填 LingoX/VoxX 价格数字 + 生成 voxx 图标。
- **装机面提层**：`deploy/`(docker-compose+cluster_map+provision)、`packaging/`(installer/build/publish) 从 `avatarhub/` 提到全域层（或软链）。
- **接新 remote 前**：跑 `tools/prepush_cleanup.ps1`（字体转 LFS + 可选 filter-repo 抹历史大对象）→ `git gc` → 建 remote → push；`website/` 部署前 `npm run build` 自验。
- **闭环收口**：`licensing`+`identity` 抽取通了，"官网↔SKU↔license↔交付"业务闭环才真正闭合（Phase 5 末目标）。
