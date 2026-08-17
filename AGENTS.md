# AGENTS.md — BOUNDLESS 母仓（机器先读）

> 建于 2026-08-17 项目分家轮。本仓定位已收窄：**平台共享件 + 集群部署 + 品牌 + 定价 + 控制台**。
> 产品引擎代码不在这里长——engines/ 下只剩指路 README 与历史。

## 本仓管什么
- `deploy/boundless-hud/`：U 型指挥舱 HUD、`modes.json`（集群算力模式 SSOT）、`cockpit.json`——**运行系统正在读这些文件**（hud_server/xiaojie/cluster_mode 都从本路径跑），改动前想清楚。
- `platform/spoken_style/`：真人感文本层交付包（AvatarHub → 智聊，同步纪律见其 README）。
- `brand-assets/`、`products/`（定价 SKU）、`website/`（BOUNDLESS 控制台，≠ 官网 bd2026.cc）、`docs/`。

## 引擎都搬走了（跑错棚的问题请移步）
| 引擎 | 真身 | 工作区入口 |
|---|---|---|
| avatarhub 换脸/克隆/直播 | `D:\projects\模仿音色`（本仓 engines/avatarhub 是 07-19 死快照，勿改） | `C:\模仿音色` |
| huoke 智拓 ReachX | `D:\projects\huoke`（08-17 迁出独立仓 victor2025PH/huoke，engines/huoke 已移除留 README） | `C:\智拓` |
| chengjie 智聊 | 117 机 `D:\chengjie-instances`（本仓 engines/chengjie 是旧快照） | 117 的 Cursor，联络单沟通 |

## 本克隆当前状态（P1 治理待办，2026-08-17 记）
- 本地 main 落后 GitHub：合流提交 `b366901` + huoke 移除 `869df6a` 已在远端，本地未 pull——**pull 前须处理 310 个本地脏文件**（website 139/brand-assets/deploy/docs/tools 分诊：该提交提交、该扔扔），且 pull 时设 `GIT_LFS_SKIP_SMUDGE=1`（本地 LFS 缺 website 视频对象，正常 checkout 会炸）。
- 纪律：**未推送不过夜**——本仓 08-10 的 3 条 HUD 提交在本地裸奔一周（集群 SSOT 无异地副本），08-17 已随合流推送；别再犯。
- 其他机器的克隆（117 `D:\boundless`+`D:\boundless-prod`、198 `D:\boundless`）：部署克隆只 pull 不 commit。
- `D:\_retired_boundless_latest_20260817` = 已退役的第二克隆归档（它的独有提交已全部合流上 GitHub），两周后可删。
