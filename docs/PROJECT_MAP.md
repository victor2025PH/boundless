# PROJECT_MAP — 产品↔项目↔仓库↔机器 对照表（单一真相）

> 大仓库=本仓 boundless：平台件 + 各子项目 submodule 指针。GitHub 上打开本仓即可看到全部子项目并跳转。
> 改这张表=改版图，须同步各仓 AGENTS 邻居地图。2026-08-17 拼音化 + 全私有化定版。

## 项目版图

| 项目（口头叫法） | 仓库 github.com/victor2025PH/*（全 PRIVATE） | 真身 | 工作区入口 | 管辖 |
|---|---|---|---|---|
| 换颜 / 模仿音色（承载官网 幻声/幻颜/幻影/通传） | **mofangyinse**（原 mfys20260618） | 176 `D:\projects\模仿音色` | `C:\模仿音色` | 本机 Cursor |
| 智拓 ReachX | **zhituo**（原 huoke；深历史 mobile-auto0423 已私有归档） | 176 `D:\projects\huoke` | `C:\智拓` | 本机 Cursor |
| 集群实况 / 平台（HUD·品牌·定价·共享件·控制台） | **boundless**（本仓=大仓库） | 176 `D:\boundless` | 同左 | 本机 Cursor |
| 官网 bd2026.cc | **guanwang**（另有 VPS 备份裸仓双远程） | 176 `C:\web117` | 同左 | web117 线 Cursor |
| 智聊 ChatX（含并入的通译 + 幻缘技能栈） | **zhiliao**（空仓已备，待 engines/chengjie 迁入） | 117 机（Phase B 定路径） | 117 | 117 Cursor |
| 智控 MatrixX（TG 矩阵） | **zhikong**（空仓已备，待 telegram-mtproto-ai 推入） | 117 机 `D:\telegram-mtproto-ai` | 117 | 117 Cursor |

已退役但保留的私有仓：tgkz2026、mobile-auto0423、liaotianai1201（117 telegram-ai-system 现远程，Phase B 收编）、aizk*、openclaw*、红包/支付/liaotian 系等；8 个上游 fork 无法转私（GitHub 限制），已确认无私货。

## 部署克隆台账（只 pull 不 commit）

| 机器 | 路径 | 远程 | 凭证 |
|---|---|---|---|
| 117 shengbei | `D:\boundless-prod` | `git@github-ro:victor2025PH/boundless.git` | 只读 deploy key `shengbei-boundless-ro` |
| 198 kouxing | `D:\boundless` | `git@github-ro:victor2025PH/boundless.git` | 只读 deploy key `kouxing-boundless-ro` |
| 117 shengbei | `D:\boundless`（sprint01 开发克隆，纪律违例） | 待 Phase B 推送后删除 | — |

坑位入档：117/198 的 `~/.ssh/config` 曾带 UTF-8 BOM——git 自带的 ssh 读到 BOM 直接拒载整个文件（匿名 https 时代不炸，切 ssh 才现形），2026-08-17 已剥除。**给 Windows 机写 ssh config 禁用带 BOM 的编码**。

## 铁律

1. **大仓库不装业务代码**：只有指针/地图/脚本。复制式并仓已实践证伪（engines/avatarhub 07-19 死快照腐坏实锤），勿回头。
2. **开发机不检出大仓库的 submodule**（第二工作副本=分裂脑温床）。灾备/装新机才用 `tools/clone_all.ps1` 全量检出，且检出物视为只读。
3. **未推送不过夜**；部署克隆只 pull（只读钥匙物理保证写不了）。
4. **新项目出生四步**：建拼音私仓 → AGENTS 身份声明（zhituo 迁库公告为模板）→ junction 工作区入口 → 本表登记 + `git submodule add ../<名>.git <名>`。

## 指针维护

- 刷新指针：`tools/sync_pointers.ps1`（每周或发版后手动跑；指针落后不影响子仓各自开发，只影响大仓导航视图与灾备快照的新旧）。
- 灾备/装新机：`tools/clone_all.ps1 -Root D:\restore` 一条命令全量恢复。
