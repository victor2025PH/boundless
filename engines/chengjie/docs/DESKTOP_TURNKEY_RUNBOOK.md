# 桌面版「下载即可用」运维手册

> 目标：客户下载安装包、装完、走完首启向导，四个渠道的接入方式该能用的都能用，
> 不需要我们远程配置、也不需要客户去申请任何开发者凭据。
>
> 本文只写**运维要做什么**与**出问题怎么判**。为什么这么设计写在代码注释里
> （`config/config.desktop.min.yaml` 的 `platform_login` 段、`desktop/sidecar-launcher.js`）。

## 1. 当前交付边界（0.2.11 起）

| 平台 · 方式 | 装完即用？ | 依赖什么 |
|---|---|---|
| WhatsApp · 协议多开 | ✅ 是 | baileys 边车随包，桌面壳自动拉起 |
| Messenger · 服务器托管登录 | ✅ 是 | messenger-web 边车 + 兜底 Chromium 随包 |
| Telegram · 协议多开 | ⚠️ 需一次性运维（见 §2） | pyrogram 随包；api_id 由官网派发 |
| LINE · 协议扫码 | ✅ 是 | okline 随包；Node 运行时由随包 Electron 兜底（见 §2.4） |
| 任意平台 · 真机 / 模拟器 | ⚠️ 需客户自备安卓设备 | 无法代配，属客户侧前提 |
| Telegram/WhatsApp · 网页扫码 | ❌ 不提供 | 本系统未实现，已从方式清单摘除 |

「不提供」那一项是**从弹窗里摘掉**，不是灰着显示——一个点了没反应的灰选项比没有更糟。

⚠️ **LINE 是刻意反转的产品/法务决策**（2026-07-30）：okline 是逆向库、违反 LINE ToS、
有封号风险，此前刻意不随包。现按产品要求随包分发，**封号风险由客户接受**，务必配套
**一号一指纹一代理 + 养号**防关联。技术前提（Node 运行时）已由随包 Electron 兜住，
客户端不需要客户装任何东西——见 §2.4。

## 2. Telegram 免申请 api_id：唯一的一次性运维动作

链路（全部已接好，无需改代码）：

```
首启向导「留个联系方式，免费领 7 天完整版」
  → 官网签发设备令牌 cx.…（/api/ai/device-token）
  → 客户端启动时 hosted_gateway.ensure_hosted_telegram()
  → POST {site}/api/pool/telegram-cred（Bearer 设备令牌）
  → 服务端按机器指纹粘定派发一组 api_id/api_hash
  → 注入内存 telegram.*（不落盘、不回显、用户自填的永不被覆盖）
```

### 2.1 在 VPS 上填凭据池（必须）

1. 在 [my.telegram.org](https://my.telegram.org) → API development tools 注册若干应用，
   每个拿到一组 `api_id` / `api_hash`。**一个手机号只能注册一组**，所以要几组就要几个号。
   容量按 `max` 累加：`max: 50` 表示这组最多粘定 50 台机器。
   ⚠️ 保守取值见 `platform/credpool/credpool_client.py::SAFE_MAX_ACCOUNTS_PER_API`
   （官方未公布硬上限，但公开/泄露的 api_id 会触发 `API_ID_PUBLISHED_FLOOD`）。

2. 写进官网环境（两种方式取其一，**文件优先**）：

   ```bash
   # 组数多时用文件（600 权限，不进 env dump）
   POOL_TG_CREDS_FILE=/etc/chatx/tg-creds.json
   # 少量组时直接用 env
   POOL_TG_CREDS='[{"api_id":"123456","api_hash":"…","max":50,"name":"grp-1"}]'
   ```

   非法条目会被**跳过**（宁缺勿错）；数组为空 = 池禁用（暗态，客户端静默回落自助流程）。
   文件方式支持热更新（按 mtime 缓存，改完不必重启站点）。

3. 验证：打开官网 `/console/trial` 页，应看到每组的 `used/max` 与总容量
   （数据来自 `lib/tg-cred-pool.ts::poolStats`，`api_hash` 永不出现在任何回显里）。

### 2.2 客户侧那一环：**首启领取不能跳过**

首启向导的领取步骤有「先不领，直接开始」。跳过的后果是**静默**的：
官网默认只给领过试用的指纹签发设备令牌（`AI_GATEWAY_REQUIRE_CLAIM`），
跳过 ⇒ 没有令牌 ⇒ **既没有托管 AI，也拿不到 Telegram 凭据**。

- 客户已跳过 → 让他在「会员中心」补领，**无需重启**（license 路由钩子会立即重试接入）。
- 若要彻底去掉这道闸：官网设 `AI_GATEWAY_REQUIRE_CLAIM=0`。
  代价是任何人刷随机指纹都能白嫖额度，且丢掉线索归属——**属产品决策，别默默改**。

### 2.3 自检（比逐层猜快）

```bash
python scripts/hosted_chain_doctor.py            # 只读，不打官网
python scripts/hosted_chain_doctor.py --live     # 额外真打一次派发接口
python scripts/hosted_chain_doctor.py --json     # 接监控
```

把上面四个前提逐条摊开，并给出**这一条**该做什么。多实例部署自动逐根体检。
`--live` 对本机幂等（派发按指纹粘定，同机反复调用拿回同一组），不会多占池容量。

⚠️ 该脚本跑在**源码检出**里（运维/自建客户），不随安装包分发。
打包客户的自查走界面：AI 未激活看顶栏橙条；Telegram 缺凭据看接入弹窗（会直接给
「填 API ID / Hash 保存即启用」表单）；额度与领取状态看「会员中心」。

## 2.4 LINE 的 Node 运行时：随包 Electron 兜底（客户无需装 Node）

okline 不是纯 Python：它起一个**持久 Node 子进程**加载 `ltsm.wasm` 算 X-Hmac 签名，
网关对每个请求强制校验——没有可用 node 就扫不了码，**且登录后每一次收发也会失败**
（这点最容易漏：只修登录链会得到「能登录但发不出」的最难排查形态）。

我们**不随包第二份 node.exe**，而是把 Electron 自己当 Node 20 用（`process.execPath` +
`ELECTRON_RUN_AS_NODE=1`），与 WhatsApp/Messenger 边车同款做法。已实测：随包 Electron 下
okline 的 LTSM 桥能正常 `curvekey_generate` 并算出 44 字节 X-Hmac，与真 node 无差别
（1.0s vs 0.7s）。

解析顺序（`line_protocol_login.resolve_node_runtime`，前者优先）：

1. `platform_login.line.node_path`（运维显式指定）
2. 已设好的 `LINE_NODE`
3. **PATH 上的真 node**——正统运行时、行为最可预期，故优先于 Electron
4. **随包 Electron**（壳注入 `AITR_ELECTRON_NODE`）——没装 Node 的机器靠它

于是：装了 Node 的机器行为完全不变；没装的机器自动用随包 Electron。两者都不需要客户动手。
钉的是 `LINE_NODE` 环境变量（而非给 OkLine 传 `node_path`）——因为 worker 收发、存量同步、
peer 身份解析各自另建 okline 实例且都不带 config，只有 env 这层能一次覆盖全部。

- 四条都不成立（典型：**非桌面部署**、从源码跑且机器没装 node）：接入弹窗如实显示
  「缺组件 Node.js 18+」并给安装指引，而不是挂着绿色「推荐」骗点击。
- ⚠️ `LINE_NODE` **别设成坏值**：它优先于 PATH 上的 node。后端检测到它指不到文件时会自动
  纠正回真 node，但一个**存在却不是 node** 的文件仍会被尊重并导致失败。
- ⚠️ okline 违反 LINE ToS、有封号风险：务必**一号一指纹一代理 + 养号**（见 §1）。

## 3. 出包前置（构建机上，漏了会出「装了也用不了」的包）

```bash
cd services/whatsapp-baileys && npm ci
cd ../messenger-web && npm ci
# 兜底 Chromium 必须装进 node_modules（不是默认的 %LOCALAPPDATA%\ms-playwright）
$env:PLAYWRIGHT_BROWSERS_PATH='0'; npx playwright install chromium

# LINE 协议随包：okline 必须在构建 python 环境里（build_backend 的 --collect-all okline
# 靠它抓 ltsm/*.wasm+*.js）。requirements.txt 已启用 okline，装依赖时一并装上：
#   pip install -r requirements.txt

cd ../../desktop
npm run build:backend     # PyInstaller + 安装版后端烟测（约 9 分钟）
npm run dist:win          # electron-builder；afterPack 会核对随包交付物
```

两道门禁会拦住漏件，不必靠人 review：

- `tests/test_desktop_seed_deliverable.py`（源码侧）：种子里开了的每个方式必须①本系统
  真实现了②依赖真在包里；另钉住端口漂移、随包浏览器缺失、明文令牌、托管链被关。
- `desktop/build/after-pack.js`（产物侧）：装包打完核对 `resources/` 真有边车与 Chromium，
  缺件即**打包失败**；同时拦住把本机生产号的 `sessions/`、`logs/` 打进公开安装包。

体积参考（0.2.11）：安装包 ~382 MB。其中 messenger-web 436 MB（含 headed Chromium 415 MB）、
whatsapp-baileys 82 MB；okline + ltsm WASM 桥体积可忽略（<1 MB），Node 运行时复用 Electron
故**零增量**。headless shell（269 MB）与 ffmpeg 已排除出包——桌面登录是人工交互，用不到无头版。

## 4. 故障字典

接入弹窗上的徽标就是根因码，不同的字对应完全不同的处置：

| 徽标 | 根因 | 处置 |
|---|---|---|
| 未启用 | 开关没开，或该方式本系统未实现 | 查种子 `platform_login`；未实现的应从 `modes` 摘掉 |
| 缺凭据 | 开关开了但没有 api_id/api_hash | 弹窗内自助表单可直接填；要免申请就跑 §2.3 自检 |
| 缺组件 | LINE 的 okline 没打进后端包 | 构建机 `pip install -r requirements.txt` 后重打（含 okline + WASM 桥） |
| 缺组件 Node.js 18+（LINE） | 四条 node 解析路径全不成立（多为非桌面部署） | 装机版应由随包 Electron 兜住；源码部署装 Node 18+ 或设 `LINE_NODE`（见 §2.4） |
| 服务未运行 | 开关开了、边车没起来 | 看 `<数据根>/logs/wa-sidecar.log` / `msg-sidecar.log` |
| 需运维配置 | Messenger 未启用 | 查种子 `messenger.web_enabled` |
| 不会常驻在线 | `orchestrator_enabled` 关着 | 扫上了也不托管，重启要重扫；种子里应为 true |

边车自身的状态可经桌面壳 IPC `desktop:sidecar-status` 取：
`absent`＝没随包、`port-conflict`＝端口被别家服务占着、`running-external`＝复用了已在跑的、
`failed`＝连续重启到上限后停手。

### Messenger 登录被弹回登录页

**不要**把 `MSG_BROWSER_CHANNEL` 钉成捆绑 Chromium。服务默认优先系统真 Chrome，
没装才回落随包 Chromium；捆绑版的 UA 与它自己发出的 Sec-CH-UA 自相矛盾（品牌报
Chromium、内核版本落后），是 Facebook 最容易抓的自动化信号，正是「账密验证码都对却
被弹回登录页、cookie 只剩 datr」的成因。装了 Chrome 的机器成功率明显更高——
客户反复登不上时，**让他装一个 Chrome** 是第一处置。
