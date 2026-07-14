# 项目结构、功能与进度

项目：`mobile-auto0423`
定位：真机集群自动化、社交平台获客、任务调度、风控与运营后台。

## 一、当前体积

清理后项目约 `349 MB`。主要体积来自：

```text
data/       248 MB   本地运行数据、更新包、实验数据；大截图/取证已移出
apk_repo/    32 MB   APK/更新包，不建议进 GitHub
src/         21 MB   源码
vendor/      10 MB   scrcpy/ADB 相关运行依赖
tests/       10 MB   测试
```

已移出的截图、日志、debug 和取证资料在：

```text
D:\workspace\_cleanup_quarantine_20260531\mobile-auto0423
```

## 二、目录树

```text
mobile-auto0423
├─ apk_repo/                 # APK 和更新包；建议走 Release/外部下载
├─ config/                   # 平台、设备、任务、AI、通知配置
│  ├─ apps/                  # Facebook/TikTok/Instagram 等平台配置
│  ├─ prometheus/            # 监控告警模板
│  └─ workflows/             # 任务工作流预设
├─ data/                     # 本地运行数据；真实数据不上传
├─ docs/                     # 架构、部署、运维、市场、操作文档
│  ├─ dev/                   # 开发设计和阶段计划
│  └─ runbook/               # 运维 runbook
├─ migrations/               # 数据库迁移脚本
├─ plugins/                  # 插件扩展
├─ scripts/                  # 运维、部署、测试、分析脚本
├─ src/
│  ├─ ai/                    # LLM client、线索评分、内容分析
│  ├─ analytics/             # 分析统计
│  ├─ app_automation/        # Facebook/TikTok 等 App 自动化
│  ├─ behavior/              # 合规、节奏、代理健康、人类行为模拟
│  ├─ chat/                  # 聊天相关逻辑
│  ├─ device_control/        # ADB/uiautomator2 设备控制
│  ├─ host/                  # FastAPI 后台、调度、数据库、Web 静态资源
│  ├─ leads/                 # 线索相关逻辑
│  └─ observability/         # 指标、日志、监控
├─ tests/                    # 单元测试、接口测试、E2E 测试
├─ tools/                    # 辅助工具和 APK helper
└─ vendor/                   # scrcpy/ADB 运行依赖
```

## 三、开发功能进度

| 模块 | 状态 | 说明 |
|---|---|---|
| 设备集群控制 | 已实现 | Coordinator/Worker、设备注册、心跳、WebSocket/HTTP |
| Facebook 自动化 | 已实现并扩展 | 浏览、进群、加友、打招呼、收件箱、消息请求、好友请求 |
| Smart Engage | 新增/进行中 | 帖子分析、A/B、群互动、L1/L2 评分、链式任务 |
| TikTok 自动化 | 已实现并扩展 | 浏览、关注、聊天、任务入口 |
| 任务链/调度 | 新增/进行中 | `task_chain.py`、`task_chains.yaml`、失败修复、链路建议 |
| Lead Mesh | 已实现并扩展 | canonical、dossier、handoff、webhook、客户服务 |
| AI 线索评分 | 已实现并扩展 | `fb_lead_scorer.py`、`fb_post_analyzer.py`、LLM routing |
| 风控与合规 | 已实现并扩展 | 配额、节奏、代理健康、风险冷却 |
| 运营后台 | 已实现并扩展 | dashboard、Facebook ops、平台网格、漏斗/分析 |
| 运维部署 | 已实现并扩展 | fresh worker 部署脚本、runbook、上传清单 |

## 四、当前未上传/需确认内容

这些文件仍留在本地，原因是它们可能包含真实运行状态、真实设备、通知配置或一次性排查逻辑：

```text
config/ai.yaml
config/cluster_state.json
config/device_aliases.json
config/device_registry.json
config/devices.yaml
config/notify_config.json
scripts/_*.py
```

建议处理方式：

1. 对配置文件制作 `.example` 模板，真实文件继续本地忽略。
2. 对 `scripts/_*.py` 分类：长期可用的改名放 `scripts/ops/`，一次性的移入隔离区。
3. `data/` 中仍有更新包和运行 DB，如要做干净开发仓，可把 `data/` 整体外置。

## 五、当前风险

1. `data/` 仍占较大体积，且可能含真实业务数据。
2. `apk_repo/` 不适合进 GitHub，后续建议改为 Release 下载或内部网盘。
3. `vendor/` 当前保留 scrcpy 运行依赖，若要纯源码仓，可改成安装脚本下载。
4. 全量测试依赖 RPA/外设/运行环境，提交前建议优先跑模块级测试。

## 六、建议下一步

1. 整理 `scripts/_*.py`，把可复用脚本纳入 `scripts/ops/`，临时脚本隔离。
2. 建立 `config/*.example.*`，降低真实设备配置误上传风险。
3. 把 `data/openclaw-update*.zip` 与 APK 移到 Release/外部资产目录。
4. 每周清理 `logs/`、`debug/`、`data/forensics/`、`data/fb_profile_shots/`。
