# 无界 wujie · 多机协同开发标准（五台电脑）

> 唯一真源 = GitHub 上的 `wujie`。任何机器都从它 clone、往它 push。**不允许再有"本地私版"各改各的**。
> 历史教训：telegram 曾 3 份副本、avatarhub 曾两台机器各改 —— 分叉是最大的坑，本标准就是为杜绝它。

## 0. 三原则
1. **代码进仓，运行时不进仓**：源码统一在 wujie；模型权重 / `config.yaml` 机密 / `sessions/` / `node_modules` 永不提交，靠各机 `deploy/provision` 就地补。
2. **改在仓里，跑在对的机器**：换脸/TTS 要 GPU —— 代码在 wujie 改，部署到对应 GPU 机跑。
3. **主干受保护，一切走分支 + PR**：`main` 永远可发布，只能经 PR 合入。

## 1. 每台电脑接入（各做一次）
```powershell
git lfs install
cd D:\workspace
git clone https://github.com/victor2025PH/wujie.git
cd wujie
powershell -File tools\install_hooks.ps1                                          # 装 pre-push 门禁钩子
powershell -File deploy\deploy.ps1 -Action provision                              # 只读看运行时缺口
powershell -File deploy\deploy.ps1 -Action provision -Apply -From <备份路径>       # 补 config 机密
powershell -File deploy\status.ps1 -Profile all                                   # 看本机该跑的服务
```
> 装好后**不要再保留** `模仿音色`/`telegram-mtproto-ai` 等独立老仓（已归档封存）。

## 2. 角色分工
| 机器 | 角色 | 负责区域 | 常提交代码 |
|---|---|---|---|
| 中枢(117) | 集成 + TTS 运行 | platform/ deploy/ 集成 | 是 |
| **5090 机** | **换脸/换声开发 + 调试** | **engines/avatarhub/** | 是 |
| 官网机 | website | website/ | 是 |
| 获客机 | huoke | engines/huoke/ | 是 |
| GPU 算力机(176/104/140/198) | 纯运行时 | 不写码，pull 后跑 | 否 |

## 3. 日常开发标准流程（核心循环）
```powershell
git checkout main; git pull --rebase origin main          # ① 同步主干
git checkout -b feat/avatarhub-xxx                         # ② 一任务一分支一人
#   ……在 wujie 里改……
powershell -File tools\repo_doctor.ps1                     # ③ 自检必须 FAIL=0
git add engines/avatarhub/<改动>                           # ④ 只 add 本次改动，勿 git add -A
git commit -m "feat(avatarhub): ..."
git pull --rebase origin main                              # ⑤ 推前再并主干、解冲突
git push -u origin feat/avatarhub-xxx
#   ⑥ GitHub 开 PR → 1 人评审 + doctor 绿 → squash 合并 main → 删分支
```
运行时机器在相关代码合并后：`git pull` → `deploy\status.ps1` → 需要则 `deploy\up.ps1 -Only <svc> -Force` 重启。

## 4. 防分叉/防冲突铁律
1. **一块区域同一时刻只有一个人/一个 Cursor 会话在改**；多开 Cursor 必须分不同区域/分支。
2. **勤同步、小步提交、小 PR 当天合**。
3. **绝不保留本地私版**：要改就在 wujie 分支里改、推上去。
4. 绝不直推 `main`；绝不 `git add -A` 抓无关文件；绝不提交机密/权重。
5. 依赖单向：`website/products → engines → platform`，产品间零横向 import。

## 5. avatarhub（换脸/换声）专项 · 5090 机
- **唯一真源 = wujie**；5090 上的旧 `模仿音色` 交接后封存。
- 换脸 **dev + debug 都在 5090**（它有 GPU / facefusion 环境 / 模型）：在 5090 的 wujie 副本里改 `engines/avatarhub/`，本机跑 `deploy` 起服务调试，走分支 + PR。
- **交接一次性动作**（尚未做则先做）：5090 老仓最后一次 commit+push → 中枢 `tools\sync_engine.ps1 -Engine avatarhub -Apply` 并入 wujie → 5090 改用 `git clone wujie` 开发。此后不再用 sync_engine（除非回捞历史）。

## 6. 门禁与卫生
- 推前门禁：`tools\repo_doctor.ps1` 必须 FAIL=0（已装 `pre-push` 钩子，不绿不让推）。
- 大文件走 LFS（`.gitattributes` 已配 fonts/mp4/psd 等）。
- 每周：删已合并分支、`git gc`、查新混入的 >10MB 文件。

## 7. 场景速查
| 场景 | 做法 |
|---|---|
| 改换脸 | 5090 上 `pull --rebase` → `feat/avatarhub-*` → doctor → PR |
| 两人改同一文件 | 先合前一个 PR，后者 `pull --rebase` 再改 |
| 官网+引擎都要改 | 拆两个 PR，别混一个分支 |
| GPU 机上新版 | `git pull` → `deploy\up.ps1 -Only <svc> -Force` |
| 拉冲突 | `git pull --rebase` 解冲突 → doctor 绿 → 推 |
