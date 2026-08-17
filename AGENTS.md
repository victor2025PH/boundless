# AGENTS.md — BOUNDLESS 母仓（机器先读）

> 建于 2026-08-17 项目分家轮。本仓定位已收窄：**平台共享件 + 集群部署 + 品牌 + 定价 + 控制台 + 大仓库（全部子项目 submodule 指针）**。
> 产品引擎代码不在这里长——engines/ 下只剩指路 README 与历史；子项目以 gitlink 指针挂在根目录。

## 本仓管什么
- `deploy/boundless-hud/`：U 型指挥舱 HUD、`modes.json`（集群算力模式 SSOT）、`cockpit.json`——**运行系统正在读这些文件**（hud_server/xiaojie/cluster_mode 都从本路径跑），改动前想清楚。
- `platform/spoken_style/`：真人感文本层交付包（AvatarHub → 智聊，同步纪律见其 README）。
- `brand-assets/`、`products/`（定价 SKU）、`website/`（BOUNDLESS 控制台，≠ 官网 bd2026.cc）、`docs/`。
- **大仓库层（2026-08-17）**：根目录 `mofangyinse`/`zhituo`/`guanwang`（后续 +`zhiliao`/`zhikong`）= 子项目 submodule 指针（相对 URL；**开发机刻意不检出**——第二工作副本=分裂脑温床）。版图单一真相 `docs/PROJECT_MAP.md`；指针刷新 `tools/sync_pointers.ps1`；灾备拉全 `tools/clone_all.ps1`。

## 引擎都搬走了（跑错棚的问题请移步）
| 引擎 | 真身 | 工作区入口 |
|---|---|---|
| avatarhub 换脸/克隆/直播 | `D:\projects\模仿音色`，仓 **victor2025PH/mofangyinse**（原 mfys20260618；本仓 engines/avatarhub 是 07-19 死快照，勿改） | `C:\模仿音色` |
| huoke 智拓 ReachX | `D:\projects\huoke`，仓 **victor2025PH/zhituo**（原 huoke，08-17 迁出；engines/huoke 已移除留 README） | `C:\智拓` |
| chengjie 智聊 | **代码真身就是本仓 `engines/chengjie`**（117 的 zhiliao:18799/tongyi:18899 双实例进程 + ~40 计划任务跑在 117 的 boundless 联接树上，2026-08-17 Q1 回信实证——它不是快照！）；`chengjie-instances` 仅实例运行时数据根（config/db/logs，无代码）；GitHub 空私仓 **zhiliao** 已备，待 117 按 repos 单迁入 | 117 的 Cursor，联络单沟通 |

## 本克隆当前状态（P1 治理已完成，2026-08-17 下午）
- ✅ 分裂脑已终结：310 个脏文件按 7 主题 wip 提交保全 → merge origin/main（86 冲突：67 纯 EOL + 19 真分歧**全采远端演进版**——本地 website 副本是 07 月过期版，intro 实验定稿/ledger is_test v6/cron watchdog 都在远端）→ 工作树全净、与 GitHub 同步。
- ✅ engines/avatarhub 死快照已清退（留指路 README）；engines/huoke 已迁独立仓（现名 `victor2025PH/zhituo`）；**engines/chengjie=活体生产面，勿清退勿 README 化**（2026-08-17 Q1 回信裁决：117 双实例+40 计划任务在用；zhiliao 独立仓迁移完成之前保持原样——迁移由 117 按 repos 单在维护窗执行）。
- 本仓 LFS 注意：本地缺 website 视频对象，checkout 涉及 `*.mp4` 时设 `GIT_LFS_SKIP_SMUDGE=1`（盘上是指针文件；要真身跑 `git lfs pull`）。
- 纪律：**未推送不过夜**——08-10 的 3 条 HUD 提交曾在本地裸奔一周（集群 SSOT 无异地副本），教训入档；根目录 `_*.png` 渲染产物已入 ignore。
- 其他机器的克隆（2026-08-17 回信后状态）：117 `D:\boundless`（=`D:\workspace\boundless` 联接树，**活体生产**）sprint01 分支未推送已清零、882 脏文件本周维护窗收敛；117 `D:\boundless-prod` 已 ff 到最新=零引用的预备部署克隆（Phase B 升级为唯一只读克隆）；198 `D:\boundless` 陈旧待接线。部署克隆只 pull 不 commit。
- `D:\_retired_boundless_latest_20260817` = 已退役的第二克隆归档（独有提交已全部合流上 GitHub），两周后可删。
- ✅ 2026-08-17 傍晚 Git 重组（中枢线执行）：全账号 25 个公开仓转私有（8 个上游 fork 除外，GitHub 不许 fork 转私）；仓名拼音化 mofangyinse/zhituo，官网上 GitHub=guanwang（web117 线自办）；本仓升**大仓库**（见上）；117 `D:\boundless-prod` 与 198 `D:\boundless` 改走只读 deploy key（`github-ro`=ssh.github.com:443）。坑位入档：两机 `~/.ssh/config` 曾带 UTF-8 BOM，git 自带 ssh 读到 BOM 拒载整个文件（匿名 https 时代不炸、切 ssh 才现形）——已剥除，给 Windows 写 ssh config 禁带 BOM。
