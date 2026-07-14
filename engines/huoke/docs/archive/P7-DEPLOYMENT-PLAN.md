# P7 全球手机矩阵部署方案

> 编写时间: 2026-03-13
> 状态: 等待实施
> 前置完成: Phase 6A-6D (通用引擎/APP自动化/获客工作流/设备矩阵+AI意图+Watchdog)

---

## 一、当前资产清单

### 1.1 设备状态 (2026-03-13)

| 编号 | 设备 ID | 型号 | 状态 | 备注 |
|------|---------|------|------|------|
| D1 | `7HKB6HRSHYDMIJ4X` | Xiaomi 23106RN0DA (Redmi 13C) | **online** | 已 Root |
| D2 | `7X8LJRIRHA4LT8VG` | Xiaomi 23106RN0DA | **online** | 已 Root |
| D3 | `BACIKBQ8CYCYDAHU` | Xiaomi 23106RN0DA | **online** | 已 Root |
| D4 | `EY6X856DAAORVOPB` | Xiaomi 23106RN0DA | **online** | 已 Root |
| D5 | `J7Z5TGTCDA9H7DMF` | Xiaomi 23106RN0DA | **online** | 已 Root |
| D6 | `QWSSW86HJNZTD6EI` | Xiaomi 23106RN0DA | **online** | 已 Root |
| D7 | `SW6DB68DUCYDXWUG` | Xiaomi 23106RN0DA | **online** | 已 Root |
| D8 | `YX5HMVUGDY6X4P79` | Xiaomi 23106RN0DA | **online** | 已 Root |
| D9 | `LZLNCIKB6L9559GI` | Xiaomi 23106RN0DA | **unauthorized** | 需确认 USB 调试 |
| D10 | `89NZVGKFD6BYUO5P` | Xiaomi 23106RN0DA | **offline** | 已拔除 |

**可用: 8 台 online + 1 台待授权 = 9 台**

### 1.2 软件资产 (55 个源码文件)

| 模块 | 文件数 | 核心能力 |
|------|--------|---------|
| `src/device_control/` | 4 | DeviceManager, DeviceMatrix, Watchdog |
| `src/workflow/` | 6 | WorkflowExecutor, EventBus, AcquisitionPipeline, SmartSchedule |
| `src/ai/` | 6 | LLMClient, AutoReply, Rewriter, IntentClassifier, VisionFallback |
| `src/host/` | 10 | FastAPI (100+ endpoints), WebSocketHub, Scheduler, WorkerPool |
| `src/behavior/` | 3 | HumanBehavior, ComplianceGuard |
| `src/app_automation/` | 10 | Telegram/WhatsApp/LinkedIn/Facebook/Instagram/TikTok/Twitter + 通用插件 |
| `src/vision/` | 4 | ScreenParser, AutoSelector, VisionBackend |
| `src/leads/` | 2 | LeadsStore (CRM) |
| `src/observability/` | 6 | Logging, Metrics, Alerts, Security |
| `tests/` | 14 | 432 单元测试全部通过 |
| `config/` | 10 | YAML 设备/APP/工作流配置 |

### 1.3 需要下载准备的资源

| 资源 | 用途 | 获取方式 |
|------|------|---------|
| Shamiko 模块 v1.1.1 | 隐藏 Root | GitHub LSPosed releases |
| PlayIntegrityFix 模块 | 通过 Play Integrity | GitHub chiteroman/PlayIntegrityFix |
| DeviceSpoofLab-Magisk | 设备指纹伪装 | GitHub yubunus/DeviceSpoofLab-Magisk |
| LSPosed 框架 | Xposed Hook 支持 | GitHub LSPosed/LSPosed |
| DeviceSpoofLab-Hooks | 运行时 IMEI/硬件 Hook | GitHub yubunus/DeviceSpoofLab-Hooks |
| SocksDroid APK | SOCKS5 全局代理 (备用) | GitHub nicksunfires/SocksDroid |
| redsocks | 透明代理转发 (Root 方案) | apt / 交叉编译 |
| Tailscale APK | 管理通道 VPN | Google Play / APK |
| Termux APK | 手机端脚本环境 | F-Droid |
| 目标 APP APK 集合 | 6 个 APP 的 APK 文件 | 本地 apk_repo/ |

---

## 二、实施阶段划分

### Phase 7A — 设备探测 + 基础部署 (P0 最高优先)

**目标**: 把当前 8 台在线手机全部摸底，确认 Root/Magisk 状态，安装所有目标 APP。

| 步骤 | 任务 | 验证方法 |
|------|------|---------|
| 7A.1 | 批量探测所有设备详情 (型号/Android版本/Root状态/Magisk版本/已装APP) | 脚本输出设备清单表 |
| 7A.2 | 确认每台 Magisk 版本 ≥ 24，Zygisk 是否开启 | `su -c magisk -v` |
| 7A.3 | 批量检查目标 APP 安装状态 (TG/WA/LI/TikTok/X/FB/IG) | `pm list packages` 对比 |
| 7A.4 | 缺失的 APP → 建立 `apk_repo/` 本地仓库 → ADB 批量安装 | 安装后确认包名存在 |
| 7A.5 | 系统优化：关闭动画、常亮、禁止休眠、禁用不必要通知 | 设置检查 |
| 7A.6 | 更新 `config/devices.yaml`：8 台设备全部注册 | YAML 完整 |

**产出**: 8 台设备状态清单 + 所有 APP 安装完毕 + config 更新

### Phase 7B — Root 隐藏 (P0)

**目标**: 所有 APP 检测不到 Root。

| 步骤 | 任务 | 验证方法 |
|------|------|---------|
| 7B.1 | 下载 Shamiko v1.1.1 zip | 文件存在于本地 |
| 7B.2 | 下载 PlayIntegrityFix 最新版 zip | 文件存在于本地 |
| 7B.3 | 批量推送 Shamiko 到每台手机 → Magisk 安装模块 | `adb push` + `su -c magisk --install-module` |
| 7B.4 | 配置 Shamiko 白名单模式 | `touch /data/adb/shamiko/whitelist` |
| 7B.5 | 在 Magisk DenyList 添加所有目标 APP | 脚本批量添加 |
| 7B.6 | 确认 Zygisk 开启、Enforce DenyList 关闭 | Magisk 设置检查 |
| 7B.7 | 安装 PlayIntegrityFix | Magisk 模块安装 |
| 7B.8 | 隐藏 Magisk APP (改随机包名) | Magisk 设置 → 隐藏 |
| 7B.9 | 隐藏开发者选项入口 | `settings put global development_settings_enabled 0` |
| 7B.10 | 重启所有设备 | `adb reboot` |
| 7B.11 | 验证: 安装 Root 检测 APP 测试 | RootBeer 测试全绿 |
| 7B.12 | 验证: Play Integrity 检测 | BASIC + DEVICE 通过 |

**产出**: 8 台设备全部 Root 隐藏，APP 检测不到

### Phase 7C — 设备指纹伪装 (P1)

**目标**: 每台手机对外显示为不同品牌/型号，互不关联。

| 步骤 | 任务 | 验证方法 |
|------|------|---------|
| 7C.1 | 设计 8 套设备身份 (品牌/型号/指纹) | YAML 配置文件 |
| 7C.2 | 下载安装 LSPosed 框架 | 模块加载成功 |
| 7C.3 | 下载安装 DeviceSpoofLab-Magisk | 模块加载成功 |
| 7C.4 | 下载安装 DeviceSpoofLab-Hooks (LSPosed) | LSPosed 模块激活 |
| 7C.5 | 为每台手机写入对应的 spoof 配置 | `push config → 重启` |
| 7C.6 | 验证: 每台手机打开"关于手机" | 显示伪装后的型号/品牌 |
| 7C.7 | 验证: APP 内读取设备信息 | Build.MODEL 等返回伪装值 |
| 7C.8 | 创建 `config/device_identities.yaml` 配置文件 | 8 台身份全记录 |

**产出**: 8 台 Xiaomi → 对外 Samsung/Pixel/OnePlus/Sony 等不同型号

### Phase 7D — 透明代理系统 (P1)

**目标**: 每台手机走住宅 IP 代理，APP 检测不到代理/VPN。

| 步骤 | 任务 | 验证方法 |
|------|------|---------|
| 7D.1 | 创建 `config/proxy_profiles.yaml` 代理配置 | YAML 配置完成 |
| 7D.2 | 编写代理管理模块 `src/device_control/proxy_manager.py` | 代码完成 |
| 7D.3 | 实现 iptables 透明代理方案 (Root 环境) | redsocks + iptables 规则 |
| 7D.4 | 实现备用方案: SocksDroid (非 Root 备份) | APK 安装 + 配置 |
| 7D.5 | 实现 ADB 远程切换代理 (国家/城市) | 命令行切换验证 |
| 7D.6 | 实现 IP 轮换定时器 (sticky session 到期自动换) | 定时轮换测试 |
| 7D.7 | IP 验证: 每台手机访问 ipinfo.io 确认 IP 国家 | 返回正确国家 |
| 7D.8 | WebRTC/DNS 泄露检测 | 无泄露 |

**注意**: 此阶段需要用户提供代理服务商账号。如无账号，先完成框架代码，用 mock 测试。

**产出**: 代理管理系统代码 + iptables 脚本 + YAML 配置

### Phase 7E — 环境一致性引擎 (P1)

**目标**: IP/时区/语言/GPS 自动全匹配 SIM 卡/代理国家。

| 步骤 | 任务 | 验证方法 |
|------|------|---------|
| 7E.1 | 编写 `src/device_control/env_consistency.py` | 代码完成 |
| 7E.2 | 实现时区自动设置 | `settings put global time_zone` 正确 |
| 7E.3 | 实现语言自动设置 | `settings put system system_locales` 正确 |
| 7E.4 | 实现 GPS 无痕模拟 (Root 方案) | `isMockProvider()=false` |
| 7E.5 | 实现 DNS 配置 | 代理 DNS 不泄露 |
| 7E.6 | 创建国家→配置的映射表 | YAML 配置 |
| 7E.7 | 一键命令: 输入国家代码 → 自动配置全部环境 | 端到端验证 |

**产出**: 环境一致性模块 + 国家配置映射

### Phase 7F — 自动初始化引擎 (P0)

**目标**: 新手机 USB 插入 → 全自动完成所有部署。

| 步骤 | 任务 | 验证方法 |
|------|------|---------|
| 7F.1 | 编写 `src/device_control/auto_provision.py` | 代码完成 |
| 7F.2 | 实现设备发现监听 (轮询 adb devices 变化) | 检测到新设备触发 |
| 7F.3 | 集成 Phase 7A 步骤: 系统优化 + APP 安装 | 自动完成 |
| 7F.4 | 集成 Phase 7B 步骤: Shamiko + PlayIntegrityFix | 自动完成 |
| 7F.5 | 集成 Phase 7C 步骤: 指纹分配 (从身份池取下一个) | 自动分配 |
| 7F.6 | 集成 Phase 7D 步骤: 代理配置 | 自动配置 |
| 7F.7 | 集成 Phase 7E 步骤: 环境一致性 | 自动匹配 |
| 7F.8 | DeviceMatrix 注册 + Watchdog 监控 | 自动注册 |
| 7F.9 | 健康报告输出 | 初始化结果清单 |

**产出**: 一键初始化脚本，新手机 USB 插入后全自动部署

### Phase 7G — API 端点 + 测试 (P1)

**目标**: 所有新模块有 API + 完整测试覆盖。

| 步骤 | 任务 | 验证方法 |
|------|------|---------|
| 7G.1 | ProxyManager API 端点 (切换/状态/轮换) | HTTP 测试 |
| 7G.2 | EnvConsistency API 端点 (配置/验证) | HTTP 测试 |
| 7G.3 | AutoProvision API 端点 (触发/状态/历史) | HTTP 测试 |
| 7G.4 | 设备指纹 API 端点 (查看/修改身份) | HTTP 测试 |
| 7G.5 | 单元测试: proxy_manager | pytest 通过 |
| 7G.6 | 单元测试: env_consistency | pytest 通过 |
| 7G.7 | 单元测试: auto_provision | pytest 通过 |
| 7G.8 | 全量回归测试 | 所有 tests/ 通过 |

**产出**: API 完整 + 测试全绿

### Phase 7H — 实机验证 (P0)

**目标**: 在 8 台真实手机上验证整个流程。

| 步骤 | 任务 | 验证方法 |
|------|------|---------|
| 7H.1 | 对 8 台手机执行 Phase 7A (探测+APP安装) | 设备清单表 |
| 7H.2 | 对 8 台手机执行 Phase 7B (Root隐藏) | RootBeer 全绿 |
| 7H.3 | 对 8 台手机执行 Phase 7C (指纹伪装) | "关于手机"显示不同型号 |
| 7H.4 | 对 8 台手机执行 Phase 7E (环境一致性) | 时区/语言正确 |
| 7H.5 | Watchdog 监控全部 8 台 | 健康状态正常 |
| 7H.6 | DeviceMatrix 注册全部 8 台 + 提交测试任务 | 任务分发执行 |
| 7H.7 | 每台打开 TikTok/Twitter/Telegram 验证正常使用 | APP 正常启动 |
| 7H.8 | 验证报告输出 | 8 台设备全部 PASS |

**产出**: 8 台手机全部部署验证完成

---

## 三、文件结构规划 (新增文件)

```
mobile-auto-project/
├── apk_repo/                          # 新建: APP APK 仓库
│   ├── README.md                      # APK 来源说明
│   └── (APK 文件手动放入)
├── magisk_modules/                    # 新建: Magisk 模块仓库
│   ├── shamiko.zip
│   ├── playintegrityfix.zip
│   ├── devicespooflab-magisk.zip
│   └── lsposed.zip
├── config/
│   ├── device_identities.yaml         # 新建: 8 套设备指纹身份
│   ├── proxy_profiles.yaml            # 新建: 代理配置
│   ├── country_env.yaml               # 新建: 国家→环境映射
│   ├── provision_profile.yaml         # 新建: 标准初始化配置
│   └── target_apps.yaml               # 新建: 应装 APP 列表
├── src/device_control/
│   ├── proxy_manager.py               # 新建: 代理管理
│   ├── env_consistency.py             # 新建: 环境一致性
│   ├── auto_provision.py              # 新建: 自动初始化引擎
│   └── fingerprint_manager.py         # 新建: 设备指纹管理
├── scripts/                           # 新建: 部署脚本
│   ├── provision_all.py               # 批量初始化
│   ├── verify_all.py                  # 批量验证
│   └── iptables_proxy.sh              # iptables 代理规则
└── tests/
    └── test_phase7.py                 # 新建: Phase 7 测试
```

---

## 四、关键约束与注意事项

### 4.1 不能自动化的步骤 (需用户手动)

| 步骤 | 原因 | 何时需要 |
|------|------|---------|
| USB 调试授权弹窗 | Android 安全限制，必须在设备上点"允许" | 新设备首次连接 |
| Google 账号登录 | CAPTCHA 人机验证 | APP 首次登录 |
| Magisk APP 隐藏操作 | 需要在 Magisk UI 里操作 | Phase 7B 每台一次 |
| 代理服务商账号注册 | 需要付费 | Phase 7D 之前 |

### 4.2 风险与降级策略

| 风险 | 概率 | 降级方案 |
|------|------|---------|
| Shamiko 与某些 APP 冲突 | 低 | 回退到 Magisk DenyList 原生隐藏 |
| iptables 代理不稳定 | 中 | 降级到 SocksDroid VPN 方案 |
| LSPosed Hook 导致 APP 崩溃 | 低 | 单独禁用对该 APP 的 Hook |
| 设备指纹伪装被检测 | 低 | 使用真实市面流行型号的指纹 |
| Play Integrity 不通过 | 中 | 社交 APP 不依赖 Strong Integrity |

### 4.3 实施顺序依赖

```
Phase 7A (探测+安装)
    ↓
Phase 7B (Root隐藏) — 依赖 7A 确认 Magisk 状态
    ↓
Phase 7C (指纹伪装) — 依赖 7B 的 LSPosed 安装
    ↓  ↘
    ↓   Phase 7D (代理) — 可与 7C 并行
    ↓  ↗
Phase 7E (环境一致性) — 依赖 7C + 7D 的配置
    ↓
Phase 7F (自动初始化引擎) — 集成 7A-7E 所有步骤
    ↓
Phase 7G (API + 测试)
    ↓
Phase 7H (实机验证) — 在 8 台真实手机上跑完整流程
```

---

## 五、验收标准

### Phase 7 整体完成定义

- [ ] 8 台手机全部在 DeviceMatrix 中注册并标记为 healthy
- [ ] 每台手机 Root 状态被 Shamiko 完全隐藏 (RootBeer 测试全绿)
- [ ] 每台手机显示不同的设备型号/品牌
- [ ] 每台手机的时区/语言与分配的国家一致
- [ ] 代理框架代码完成 (真实代理需要账号才能测试)
- [ ] 自动初始化引擎可对新设备一键部署
- [ ] 所有新模块有 API 端点
- [ ] 全量测试 (预计 500+) 全部通过
- [ ] 每台手机能正常打开 TikTok/Twitter/Telegram 等 APP

---

## 六、预计产出

| 指标 | 数量 |
|------|------|
| 新 Python 模块 | 4 个 (proxy_manager, env_consistency, auto_provision, fingerprint_manager) |
| 新配置文件 | 5 个 YAML |
| 新 API 端点 | ~15 个 |
| 新测试 | ~60-80 个 |
| 部署脚本 | 3 个 |
| 部署完成设备 | 8 台 |
