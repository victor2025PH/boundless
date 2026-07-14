# OpenClaw 手机集群自动化系统

[![tests](https://github.com/victor2025PH/mobile-auto0423/actions/workflows/tests.yml/badge.svg)](https://github.com/victor2025PH/mobile-auto0423/actions/workflows/tests.yml)

> FB / Messenger / TikTok 等 7 平台**真机 RPA 自动化**系统。主控 + 多设备集群。

---

## 🚀 我是...

| 我是谁 / 我要做啥 | 看这里 |
|------------------|--------|
| **第一次来 / 想知道能干啥** | [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) — 业务能力矩阵 |
| **要运维 / 后台打不开 / 程序崩了** | [`docs/SYSTEM_RUNBOOK.md`](docs/SYSTEM_RUNBOOK.md) — 启停/诊断/故障字典 |
| **开发改代码 / 看架构** | [`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md) + [`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md) |
| **部署到新机 / 安装配置** | 下方"快速开始" + [`scripts/setup/`](scripts/setup/) |
| **找历史文档 / 旧 plan** | [`docs/_INDEX.md`](docs/_INDEX.md) |

## ⚙️ 日常启停（根目录三件套 + 一键迁移）

```bat
start.bat     :: 启动 service_wrapper（生产推荐）
stop.bat      :: 优雅停止
status.bat    :: 5 项巡检 + 退出码 (0=GO / 1=DEGRADED / 2=DOWN)
migrate.bat   :: 一键从非标准启动 (uvicorn 直起) 切到 service_wrapper
```

启动配置：编辑 [`config/launch.env`](config/launch.env)（端口/绑定地址/TLS）。

后台地址：**http://localhost:8000/dashboard** ⚠️ 用 `localhost`，不要用 `192.168.x.x`（详见 RUNBOOK F1）。

---

## 项目简介

基于 ADB + uiautomator2 的分布式手机集群管理与自动化平台。支持多 Worker 节点、24/7 无人值守运行、AI 驱动的智能操控。

**相关仓库**: [`victor2025PH/telegram-mtproto-ai`](https://github.com/victor2025PH/telegram-mtproto-ai) — 配套的 contacts/handoff 跨平台主骨架（含 Telegram/LINE/Android Messenger RPA runner）。两 repo 代码独立，通过 contacts 子系统的 Messenger→LINE 引流主线业务衔接。详见 [`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md)。

## 核心功能

- **集群管理**: Coordinator + Worker 分布式架构，ZeroTier 跨网段通信
- **设备控制**: ADB/uiautomator2 双通道
- **应用自动化**: Telegram、WhatsApp、TikTok、Facebook、Instagram、LinkedIn、Twitter
- **AI 驱动**: DeepSeek/Gemini/Ollama LLM 意图识别、智能回复、视觉分析
- **防掉线**: Watchdog + HealthMonitor + 预测性维护，L0-L4 恢复等级
- **合规引擎**: 配额管控、人类行为模拟、VPN 地理伪装
- **实时流**: Scrcpy H.264 直播 + WebSocket 群控（<20ms 延迟）
- **数据分析**: 转化漏斗、ROI 面板、A/B 实验、增长分析

完整能力矩阵 → [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md)。

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Python 3.13 + FastAPI + uvicorn |
| 设备控制 | uiautomator2 + ADB |
| 流媒体 | Scrcpy + WebCodecs H.264 |
| 数据库 | SQLite (WAL) + PostgreSQL（L2 中央客户画像，可选） |
| AI/LLM | DeepSeek / Gemini / Ollama |
| 集群通信 | HTTP + WebSocket + ZeroTier |
| 前端 | 原生 JS + Chart.js |

## 架构（高层）

```
                    ┌─────────────────┐
                    │   Coordinator   │
                    │  (本机或局域网)  │
                    │   Port 8000/18080│
                    └────────┬────────┘
                             │ HTTP/WS 心跳
              ┌──────────────┼──────────────┐
              │              │              │
    ┌─────────▼──┐  ┌───────▼────┐  ┌──────▼─────┐
    │ Worker-01  │  │ Worker-02  │  │ Worker-N   │
    │ N 台手机   │  │  (扩展)    │  │  (扩展)    │
    └────────────┘  └────────────┘  └────────────┘
```

详细进程拓扑/任务派发时序 → [`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md)。

## 项目结构（顶层）

```
mobile-auto0423/
├── start.bat / stop.bat / status.bat / migrate.bat   # 日常运维
├── server.py / service_wrapper.py                    # 启动入口
├── src/                  # 业务代码（host / device_control / app_automation / ai / behavior / workflow）
├── config/               # YAML/JSON 配置 + launch.env
├── docs/                 # 文档（runbook/ dev/ archive/ 子分类）
├── scripts/              # 工具脚本（ops/ setup/ migrations/ _archive/）
├── tests/                # 测试用例
├── tools/                # 命令行工具
├── data/                 # 运行时数据（含 SQLite）
├── logs/                 # 日志
├── vendor/               # 第三方 binary（DLL / scrcpy.exe）
├── plugins/              # 插件
├── apk_repo/             # APK 仓库
├── debug/                # 调试 dump
├── temp/                 # 临时文件
└── migrations/           # DB 迁移
```

## 快速开始

### 环境要求

- Python 3.10+（推荐 3.13）
- ADB (Android Debug Bridge) — 已在 PATH
- 已开启 USB 调试的 Android 设备

### 安装与启动

```bash
# 克隆项目
git clone <repo-url>
cd mobile-auto0423

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填写 DEEPSEEK_API_KEY 等

# 启动（推荐方式：service_wrapper 守护）
start.bat
# 或裸启（无守护，开发调试用）：
# python server.py

# 验证
status.bat
# 浏览器打开 http://localhost:8000/dashboard
```

详细启停/重启/故障 → [`docs/SYSTEM_RUNBOOK.md`](docs/SYSTEM_RUNBOOK.md)。

### Worker 部署

```bash
# 方式1: OTA 自动更新
powershell scripts/ops/push_worker_update_from_coordinator.ps1

# 方式2: 手动拉取
powershell scripts/ops/worker_pull_update.ps1
```

### Windows 自启

```bash
# 注册计划任务（OpenClaw-Worker），用户登录时自动起 service_wrapper
scripts/ops/setup_autostart.bat
```

## API 文档

启动后访问 http://localhost:8000/docs 查看 Swagger 文档。

**29 个路由模块**涵盖:
- 设备管理: `/devices/*`
- 集群协调: `/cluster/*`
- 任务执行: `/tasks/*`, `/batch/*`
- 应用自动化: `/tiktok/*`, `/facebook/*`, `/macros/*`
- AI 集成: `/ai/*`
- 数据分析: `/analytics/*`
- 安全管理: `/security/*`, `/risk/*`
- 监控告警: `/monitoring/*`, `/notify/*`

## 测试

```bash
python -m pytest tests/ -x -q --ignore=tests/e2e -k "not real"
# commit 前必跑（CLAUDE.md 约定）
```

## 开发团队

AI 协同开发:
- **Claude** — 架构设计与重构
- **DeepSeek** — 推理与核心开发
- **千问** — DevOps 与高级功能

## 许可证

Private — All rights reserved.
