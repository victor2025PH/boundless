# 实施32：WP-1 纯云起步档 cloud_light（零 GPU 一键可跑）

> 日期：2026-08-16 ｜ 规格来源：`docs/开发文档_智聊商业化_三视角实施规格_2026-08.md` §WP-1
> 约束：遵守共享树纪律；feature 默认关（无 env 即 no-op）；不动生产实例默认行为。

## 1. 交付物

| 文件 | 内容 |
|---|---|
| `engines/chengjie/config/profiles/cloud_light.yaml`（新） | 能力预设档：`deploy.profile` 标记 + 全部 LAN/infra 依赖键显式关 + 视觉/嵌入/本地 MT 端点清空 + 翻译云引擎序 + TTS 后端钉 edge_tts。**零内网 IP**（有门禁） |
| `engines/chengjie/src/utils/deploy_profile.py`（新） | 纯函数模块：`load_profile`（名字白名单防穿越/软失败）、`list_profiles`、`active_profile`、`capability_snapshot`（各能力 on/off/degraded 快照，只读 config 零网络零密钥） |
| `engines/chengjie/src/utils/config_manager.py` | `_ensure_deploy_profile()` 首启播种钩子（挂 `load()` 内 `_ensure_baseline` 之后、validate 之前）+ `_seeded_fresh` 全新播种标记（`_ensure_seeded` 真拷贝时置位） |
| `engines/chengjie/src/web/routes/unified_inbox_setup_routes.py` | `GET /api/setup/deploy-profile` 只读就绪自检端点（当前档位 + available + capabilities 一屏；supervisor/壳 Bearer 鉴权同族端点口径） |
| `engines/chengjie/desktop/backend-launcher.js` | 打包态（bundled 分支）注入 `AITR_DEPLOY_PROFILE=cloud_light`（外部 process.env 显式设置可覆盖） |
| `engines/chengjie/desktop/build/build_backend.py` | PyInstaller DATAS 增 `config/profiles/` 目录（随包分发落 `_internal/config/profiles/`） |
| `engines/chengjie/desktop/test/backend-launcher.test.js` | 新增 2 断言：打包态默认档 + env 可覆盖（顶部先清宿主 env 保确定性） |
| `engines/chengjie/tests/test_deploy_profiles.py`（新门禁，12 例） | 见 §3 |
| `engines/chengjie/tests/test_admin_route_inventory.py` | 基线登记 `/api/setup/deploy-profile GET` |

## 2. 设计决策（为什么是这个形状）

1. **首启一次性播种，不是运行时 merge 层**（规格技术要点原样落地）：预设内容进
   `config.local.yaml` overlay（`save_overlay_patch`＝ruamel 保注释写入），事后可检视、
   可被后续运营开关覆盖——预设是起点不是锁。
2. **三道闸，缺一不应用**：
   - env `AITR_DEPLOY_PROFILE` 显式声明——生产双实例（zhiliao/tongyi）与开发态无此
     env，钩子恒 no-op，「不动生产默认行为」由此机制保证而非靠约定；
   - `_seeded_fresh`＝本次 config.yaml 为**全新播种**——升级安装/已有配置的机器永不
     被应用，用户对任何键表达过的选择（含手改 YAML）都保留；
   - `deploy.profile` 标记幂等——同进程热重载会重进 `load()`，标记挡住二次落盘。
3. **与 `feature_registry` 机械对齐**：cloud_light 钉死的键 = 注册表 C 类中
   reason∈{lan,infra} 的全部 10 键（avatar_voice/selfie/realtime_voice/speech_emotion/
   ai.fallback/gpu_watermark/cloud_credentials/三 RPA）。risk/pending/safety 类
   （proactive/bazi/monetization/contacts/l2_autosend.deliver）**刻意不进预设**——那些是
   与部署拓扑正交的产品决策，本就默认关。门禁逼两源同步：注册表新增 LAN 功能，
   预设档不跟就红。
4. **「零常驻红灯」做成机制而非巡检**：运行时健康组件里 LAN 相关的只有
   `audio`（`audio_probe_target` 守门：voice_recognition 关→不探）与
   `avatar_voice`（`avatar_probe_target` 守门：avatar_voice 关→不探），探针决策函数
   返回空即整组件不出现；`gpu_watermark`/`cloud_credentials` 各自 enabled 守门；
   `alert_link` 设计上 warn-only 永不红。上述判定全部进门禁
   （`test_cloud_light_silences_lan_probes`）。剩余组件（db/ai/license/channels/workers）
   是真依赖——cloud_light 下红了是真问题，应当报。
5. **就绪自检端点零 i18n 词条**：返回结构化枚举（state=on/off/degraded + 机器字段），
   文案翻译属消费方（WP-2 向导）职责；避免为一个尚无 UI 的读面造词条。响应零密钥
   （门禁断言 `api_key` 不出现在 capabilities）。
6. **桌面默认档=cloud_light** 的影响面：桌面包新装机器的种子（`config.desktop.min.yaml`）
   本就不配任何 LAN 键（缺省=关），预设把「隐式关」变「显式关+可检视」，对新装机
   **有效行为零变化**；升级安装因 `_seeded_fresh` 闸门完全不被触碰（JS 测试钉住
   env 注入与可覆盖性）。

## 3. 验证

**新门禁 `tests/test_deploy_profiles.py`（12/12 绿）**：
- ① 预设目录零内网 IP（RFC1918 正则扫 `config/profiles/` 全部文件；规格验收项）；
- ② `deploy.profile` 标记=文件名；registry lan/infra 键全部显式 false；
  `load_profile` 名字消毒（`../`/不存在/空 → `{}`）；
- ③ 预设合并进 example 基线后：audio/avatar 探针静默、gpu_watermark/cloud_credentials
  关、嵌入不配、翻译序=["ai"] 且 ollama_mt 无端点、vision 零 LAN 端点、本地容灾关、
  TTS=edge_tts；`capability_snapshot` 状态断言 + degraded 语义自证（开了缺依赖必须
  报 degraded，补齐翻 on——探测器不是摆设）；
- ④ 播种语义端到端（真 ConfigManager，AITR_CONFIG_PATH 指 tmp 密闭）：全新播种+env
  → overlay 落 marker 与关键键，二次 load 字节不变（幂等）；既有 config+env → 不应用；
  全新播种无 env → 不应用（生产形态零变化）；env 指向坏档 → 软 no-op 启动照常；
- ⑤ 端点只读契约（自建 app）：profile/available/capabilities 齐 + 响应零密钥字段。

**邻居定向回归**（按导入关系选靶）：ConfigManager/种子/overlay/setup 路由/路由清单/
桌面种子门禁等 18 文件 —— 237 过 2 跳 1 红；其中 1 红
`test_baseline_reconcile.py::test_runs_even_when_validation_fails_first_boot` 经
stash 二分确认为**既有红**（暂存本线全部 config_manager 改动后照样红；系他线改了
桌面首启校验语义、pin 测试未跟上），按共享树纪律报告不默改。

**CJK ratchet + 静态资产路径门禁**：10/10 绿（新端点零硬编码中文响应；新模块全
`Path(__file__)` 绝对路径）。

**桌面 `npm test` 全量**：全绿（backend-launcher 52+9，含本批 2 个新断言）。

**gate_sweep**：725 过 4 红；4 红均为他线前端 ratchet 既有红（`unified_inbox.html`
×3 / `unified-inbox.css` ×1，红灯账本首见 94.4h/46.6h/14.3h/1.6h，本线未触碰任何
相关文件；94.4h 的 `test_workspace_emoji_ratchet` 已被账本点名 UNCLAIMED>48h，
归属他线待认领）。

**全量回归**（`scripts/regression.ps1`，生产在线自动降优先级+并行封顶 4，52min）：
**19347 过 / 67 红 / 36 跳**。67 红逐类归属核对，**零项属本线**：
- 与本线导入图相交的两组嫌疑均经 **stash 二分实证排除**（暂存本线改动后原样红）：
  `test_baseline_reconcile`（桌面首启校验语义被他线改动、pin 未跟上）与
  `test_unified_inbox_stage1` ×7（聚合器注册序 pin / 气泡拆分 / 模板断言——
  `unified_inbox_routes.py`/`reply_split.py`/`sender.py`/`unified_inbox.html` 均为
  他线在途脏文件）；
- 其余 59 项全部落在 sibling 活跃编辑面：`test_workspace_i18n_render` ×30 +
  nav/sidebar/feature_gate（导航隐藏线，i18n packs + nav_schema 在途）、前端
  ratchet ×5（红灯账本首见 1.6~94.4h）、whatsapp ingest/translation memory/
  tg login codes/persona wiring/alert catalog 等（对应 src 文件全在他线脏清单）。
按共享树纪律报告不默改；>48h UNCLAIMED 的 `test_workspace_emoji_ratchet` 已由
红灯账本点名待他线认领。

## 4. 规格验收框回填状态

| 验收项 | 状态 | 说明 |
|---|---|---|
| 干净 VM 安装→首条 AI 回复中位 <30min（2 次取中位） | ⏳ 待实测 | 需干净 Win10/11 VM（本机是生产机，不适合装测）。程序：装桌面包（launcher 已注入 cloud_light）→ 首启查 `/api/setup/deploy-profile` 应答 `profile=cloud_light` → 绑 TG → Saved Messages 发测试消息计时 |
| cloud_light 档 ops-overview 零常驻红灯、webhook 24h 零误报 | ⏳ 待实测（机制已门禁化） | 探针静默判定已进 `test_cloud_light_silences_lan_probes`（机制层证明）；24h 实机观察随 VM 冒烟一并做 |
| 预设文件零内网 IP（静态门禁扫 `config/profiles/`） | ✅ | `test_profiles_dir_has_no_private_ip`，CI 常驻 |
| 现有全量回归不红（预设不改默认行为） | ✅（本线涉及面） | 本线全部涉及面测试绿；「预设不改默认行为」另有专项门禁（无 env 不应用/既有配置不应用）。树上既有红归属他线（见 §3，报告不默改） |

## 5. 运维/后续接线要点

- **演示 VM（WS-2）吃狗粮**：首启前设 `AITR_DEPLOY_PROFILE=cloud_light`（机器级 env
  或启动脚本），首启自动播种；已装过的机器手动把
  `config/profiles/cloud_light.yaml` 内容合并进实例 overlay 即可（纯 YAML）。
- **就绪自检**：`GET /api/setup/deploy-profile`（supervisor 或桌面壳 Bearer）——
  WP-2 向导与支持排障的读面；`capabilities.*.state`=on/off/degraded。
- **换档/退档**：预设只是 overlay 里的普通键，任何后续开关写入（能力面板/向导）
  按既有优先级覆盖；删 overlay 对应键即回代码默认。
- **新增 LAN 依赖功能时**：进 `feature_registry`（C 类 lan/infra）后，
  `test_cloud_light_pins_all_registry_lan_keys` 会红，提示往 cloud_light 补显式关。
- **生产双实例**：无 `AITR_DEPLOY_PROFILE` env、config 非全新播种，双闸皆不满足，
  钩子恒 no-op（有专项门禁钉住）。本批 `.py` 已搭 sibling 2026-08-16 22:57 重启
  便车装载进 zhiliao——只读探针实证：`GET /api/setup/deploy-profile` 无凭证返
  **401**（路由已注册+鉴权在岗；未装载态是 404）、`/login` 200 实例健康，
  未额外消耗重启窗口。

## 6. 明确不做（规格「范围（不做）」对照）

- 不改任何降级逻辑本身（语音回落 edge/视觉多端点回落/嵌入退关键词/翻译引擎序
  全部沿用既有软失败设计，本批零触碰）；
- 不做 Linux/Docker（P2）；
- 不触碰 `voice_prerender.DEFAULT_BASE_DIR` 相对路径例外（成对门禁守护，语音关闭
  路径不经过它）。
