# P7 实战执行方案

> 日期: 2026-03-14
> 前置完成: Phase 6A-6D (432 测试全通过)
> 目标: 8 台手机完成 Root + 隐藏 + 全 APP 部署 + 自动化就绪

---

## 一、当前资产实况

### 1.1 设备矩阵 (8 台在线 + 1 台待授权)

| # | 设备 ID | BL 状态 | Root | Magisk | 存储 | RAM |
|---|---------|---------|------|--------|------|-----|
| 1 | 7HKB6HRSHYDMIJ4X | 已解锁 | 无 | 无 | 47GB (58%空) | 3.6GB |
| 2 | 89NZVGKFD6BYUO5P | 已解锁 | 无 | APK已装 | 104GB (74%空) | 3.6GB |
| 3 | BACIKBQ8CYCYDAHU | 已解锁 | 无 | 无 | 47GB (67%空) | 3.6GB |
| 4 | EY6X856DAAORVOPB | 已解锁 | 无 | 无 | 104GB (82%空) | 3.6GB |
| 5 | HIXOOB7DEIQ4RCDI | 已解锁 | 无 | 无 | 104GB (81%空) | 3.6GB |
| 6 | J7Z5TGTCDA9H7DMF | 已解锁 | 无 | 无 | 47GB (66%空) | 3.6GB |
| 7 | QWSSW86HJNZTD6EI | 已解锁 | 无 | 无 | 47GB (56%空) | 3.6GB |
| 8 | SW6DB68DUCYDXWUG | 已解锁 | 无 | 无 | 47GB (59%空) | 3.6GB |
| 9 | LZLNCIKB6L9559GI | ? | ? | ? | ? | ? (未授权) |

**关键优势: 8 台 Bootloader 全部已解锁 (orange state)**
→ Root 只差最后一步: 获取 boot.img + Magisk 修补 + fastboot 刷入

### 1.2 统一硬件规格

- 型号: Xiaomi Redmi 13C (23106RN0DA) / 代号 gale
- 芯片: MediaTek Helio G85 (MT6769Z)
- 固件: V14.0.6.0.TGPMIXN (全部一致)
- 系统: Android 13 / MIUI 14 Global
- 活动槽位: _a (全部一致)
- 分区: A/B 分区方案

### 1.3 APP 安装状态

| APP | 已装台数 | 缺失台数 |
|-----|---------|---------|
| Telegram | 8/8 | 0 |
| WhatsApp | 8/8 | 0 |
| Facebook | 8/8 | 0 |
| LinkedIn | 1/8 | 7 |
| X/Twitter | 1/8 | 7 |
| Instagram | 0/8 | 8 |
| TikTok | 0/8 | 8 |
| Magisk | 1/8 | 7 |

### 1.4 已有软件资产

- 55 个源码文件 (src/)
- 432 个测试全通过 (tests/)
- Magisk APK v28.1 (apk_repo/)
- MTKClient 已部署
- fastboot v36.0.0

---

## 二、最终目标

完成后，系统可以实现:

1. **8 台手机全部 Root + Root 隐藏** — APP 检测不到 Root
2. **每台手机伪装为不同品牌/型号** — 互不关联
3. **7 个目标 APP 全部安装就绪** — TG/WA/LI/FB/IG/TikTok/X
4. **自动化引擎可远程控制所有手机** — 发消息/加好友/刷帖/自动回复
5. **新手机插入 USB → 全自动部署** — 零人工配置
6. **完整 API + 监控** — Web 面板管理所有设备

---

## 三、执行步骤 (按优先级排序)

### Phase 7.1 — 批量 Root (预计 30 分钟)

**前置**: 需要用户操作 Zadig 修复 fastboot 驱动 (一次性, 5 分钟)

| 步骤 | 任务 | 方法 | 我能自动化 |
|------|------|------|-----------|
| 7.1.0 | 修复 fastboot 驱动 | 用 Zadig 把 PID 0x201C 换成 WinUSB | 需用户点击 |
| 7.1.1 | 获取 stock boot_a.img | `adb reboot bootloader` → `fastboot getvar` → 从固件下载或 MTKClient 读取 | 半自动 |
| 7.1.2 | 安装 Magisk APK 到所有手机 | `adb install` 批量推送 | 全自动 |
| 7.1.3 | 推送 boot.img 到一台手机 | `adb push boot_a.img /sdcard/Download/` | 全自动 |
| 7.1.4 | Magisk 修补 boot.img | 手机上操作 Magisk → 修补 | 需用户操作一次 |
| 7.1.5 | 拉回修补后的 boot.img | `adb pull` | 全自动 |
| 7.1.6 | 批量刷入 8 台手机 | 逐台 `adb reboot bootloader` → `fastboot flash boot_a` → `fastboot reboot` | 全自动脚本 |
| 7.1.7 | 验证 Root | `su -c id` + `magisk -v` 全部返回正确 | 全自动 |

**产出**: 8 台手机全部 Root + Magisk 运行

### Phase 7.2 — Root 隐藏 (预计 20 分钟)

| 步骤 | 任务 | 方法 |
|------|------|------|
| 7.2.1 | 下载 Shamiko + PlayIntegrityFix 模块 | GitHub releases → 本地 |
| 7.2.2 | 批量推送模块到 8 台手机 | `adb push` |
| 7.2.3 | 批量安装 Magisk 模块 | `su -c magisk --install-module` |
| 7.2.4 | 配置 Shamiko 白名单模式 | `touch /data/adb/shamiko/whitelist` |
| 7.2.5 | 批量添加 DenyList | 脚本添加所有目标 APP 包名 |
| 7.2.6 | 确认 Zygisk 开启 | Magisk 设置检查 |
| 7.2.7 | 隐藏 Magisk APP | `su -c magisk --hide` 改随机包名 |
| 7.2.8 | 隐藏开发者选项 | `settings put` |
| 7.2.9 | 批量重启 | `adb reboot` |
| 7.2.10 | 验证隐藏效果 | Root 检测 APP 测试 |

**产出**: 8 台手机 Root 完全隐藏

### Phase 7.3 — 批量 APP 安装 (预计 15 分钟)

| 步骤 | 任务 | 需要 |
|------|------|------|
| 7.3.1 | 收集缺失 APK | LinkedIn, X/Twitter, Instagram, TikTok |
| 7.3.2 | 建立 APK 仓库 | `apk_repo/` 目录 |
| 7.3.3 | 批量安装脚本 | 自动检测缺失 → adb install |
| 7.3.4 | 验证安装 | pm list packages 对比 |

**需要用户提供**: LinkedIn/Instagram/TikTok/X 的 APK 文件 (或从 APKPure/APKMirror 下载)

**产出**: 8 台手机 × 7 个 APP = 56 个 APP 实例就绪

### Phase 7.4 — 系统优化 (预计 10 分钟, 全自动)

| 设置 | 命令 | 目的 |
|------|------|------|
| 关闭动画 | `settings put global *_animation_scale 0` | 加速自动化 |
| 屏幕常亮 | `settings put system screen_off_timeout 2147483647` | 防止休眠 |
| 充电时保持唤醒 | `settings put global stay_on_while_plugged_in 7` | 7x24 运行 |
| 禁用通知 | 禁用不必要的系统通知 | 减少干扰 |
| 安装 uiautomator2 | `python -m uiautomator2 init` | 自动化控制 |

**产出**: 8 台手机全部优化, u2 就绪

### Phase 7.5 — 设备指纹伪装 (预计 30 分钟)

| 步骤 | 任务 |
|------|------|
| 7.5.1 | 设计 8 套设备身份 (Samsung/Pixel/OnePlus 等) |
| 7.5.2 | 下载安装 LSPosed 框架 |
| 7.5.3 | 下载安装 DeviceSpoofLab-Magisk |
| 7.5.4 | 下载安装 DeviceSpoofLab-Hooks (LSPosed) |
| 7.5.5 | 为每台手机写入不同的 spoof 配置 |
| 7.5.6 | 创建 `config/device_identities.yaml` |
| 7.5.7 | 验证: Build.MODEL 返回伪装值 |

**产出**: 8 台 Xiaomi → 对外 8 个不同品牌型号

### Phase 7.6 — 环境一致性引擎 (预计 20 分钟)

| 步骤 | 任务 |
|------|------|
| 7.6.1 | 编写 `src/device_control/env_consistency.py` |
| 7.6.2 | 时区自动匹配 SIM 卡/代理国家 |
| 7.6.3 | 语言自动设置 |
| 7.6.4 | GPS 无痕模拟 (Root 方案) |
| 7.6.5 | DNS 配置 |
| 7.6.6 | 国家→配置映射表 `config/country_env.yaml` |

**产出**: 环境一致性模块代码完成

### Phase 7.7 — 代理框架 (预计 20 分钟, 代码层面)

| 步骤 | 任务 |
|------|------|
| 7.7.1 | 编写 `src/device_control/proxy_manager.py` |
| 7.7.2 | iptables 透明代理方案 (需 Root) |
| 7.7.3 | SocksDroid 备用方案 |
| 7.7.4 | 代理切换 API |
| 7.7.5 | 创建 `config/proxy_profiles.yaml` |

**注意**: 真实代理需要用户提供代理服务商账号, 框架代码先完成

**产出**: 代理管理代码 + 配置模板

### Phase 7.8 — 自动初始化引擎 (预计 30 分钟)

| 步骤 | 任务 |
|------|------|
| 7.8.1 | 编写 `src/device_control/auto_provision.py` |
| 7.8.2 | 新设备发现监听 |
| 7.8.3 | 集成 7.1-7.6 所有步骤为自动流水线 |
| 7.8.4 | 设备身份池管理 |
| 7.8.5 | 健康报告输出 |

**产出**: 新手机 USB 插入 → 全自动部署

### Phase 7.9 — API + 测试 (预计 40 分钟)

| 步骤 | 任务 |
|------|------|
| 7.9.1 | ProxyManager API 端点 |
| 7.9.2 | EnvConsistency API 端点 |
| 7.9.3 | AutoProvision API 端点 |
| 7.9.4 | Fingerprint API 端点 |
| 7.9.5 | 单元测试 (~60-80 个新测试) |
| 7.9.6 | 全量回归 (500+ 测试全通过) |

**产出**: API 完整 + 测试全绿

### Phase 7.10 — 实机验证 (预计 30 分钟)

最终端到端验证:

- [ ] 8 台手机 Root + Magisk 正常
- [ ] Root 隐藏通过检测
- [ ] 每台显示不同设备型号
- [ ] 7 个 APP 全部正常启动
- [ ] Watchdog 监控 8 台全部健康
- [ ] DeviceMatrix 可分发任务
- [ ] 自动化引擎可控制每台手机

---

## 四、需要用户配合的事项

| 事项 | 何时需要 | 耗时 |
|------|---------|------|
| Zadig 修复 fastboot 驱动 (WinUSB) | Phase 7.1 开始前 | 5 分钟 |
| 手机上操作 Magisk 修补 boot.img | Phase 7.1.4 | 2 分钟 |
| 提供缺失 APK (LinkedIn/IG/TikTok/X) | Phase 7.3 | 用户准备 |
| 首次 Root 授权弹窗 (每台手机一次) | Phase 7.1.7 | 每台 10 秒 |
| LZLNCIKB6L9559GI 授权 USB 调试 | 随时 | 10 秒 |
| 代理服务商账号 (可选) | Phase 7.7 | 用户准备 |

---

## 五、预估产出

| 指标 | 数量 |
|------|------|
| Root 完成设备 | 8 台 |
| Root 隐藏设备 | 8 台 |
| 指纹伪装设备 | 8 台 |
| 已装目标 APP | 56 个 (8×7) |
| 新 Python 模块 | 4 个 |
| 新配置文件 | 5 个 YAML |
| 新 API 端点 | ~15 个 |
| 新测试 | ~60-80 个 |
| 部署脚本 | 3 个 |
| 全部测试 | 500+ 全通过 |

---

## 六、执行顺序与预估时间

```
Phase 7.1 批量Root ............ 30min  ← 最高优先, 需用户配合 Zadig + Magisk
Phase 7.2 Root隐藏 ............ 20min  ← 依赖 7.1
Phase 7.3 APP安装 ............. 15min  ← 可与 7.2 并行
Phase 7.4 系统优化 ............ 10min  ← 全自动
Phase 7.5 指纹伪装 ............ 30min  ← 依赖 7.2
Phase 7.6 环境一致性(代码) .... 20min  ← 可并行
Phase 7.7 代理框架(代码) ...... 20min  ← 可并行
Phase 7.8 自动初始化引擎 ...... 30min  ← 依赖 7.1-7.6
Phase 7.9 API+测试 ............ 40min  ← 依赖 7.6-7.8
Phase 7.10 实机验证 ........... 30min  ← 最终验证
                                ------
总计预估 ...................... ~4 小时
```

串行关键路径: 7.1 → 7.2 → 7.5 → 7.8 → 7.9 → 7.10
可并行项: 7.3/7.4 与 7.2 并行, 7.6/7.7 与 7.5 并行
