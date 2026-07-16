# 无界 wujie · 单人多机开发标准（一个人 · 五台电脑）

> 场景：**所有开发都是你一个人**，只是分布在 5 台电脑上（同一个 GitHub 账号，你全权掌控）。
> 唯一真源 = GitHub 上的 `wujie`。每台电脑都从它 clone、往它 push。
> 最大风险不再是"多人冲突"，而是**你在一台机器改了没同步，就去另一台又改** → 两台分叉。
> 历史教训：telegram 曾 3 份副本、avatarhub 曾两台机器各改。本标准就是为杜绝它。

## ★ 黄金铁律（只要守住这一条，多机就不会乱）
> **每台电脑：开工先 `git pull --rebase`，收工先 `commit` + `push`。**
> **离开一台电脑前，不要留任何"没推上去"的改动**（哪怕是半成品，也先 WIP 提交推上去）。

## 0. 三原则
1. **代码进仓，运行时不进仓**：源码统一在 wujie；模型权重 / `config.yaml` 机密 / `sessions/` / `node_modules` 永不提交，各机 `deploy/provision` 就地补。
2. **改在仓里，跑在对的机器**：换脸/TTS 要 GPU —— 代码在 wujie 改，部署到对应 GPU 机跑。
3. **main 直推但受保护**：单人无需 PR，直接 `push` 到 `main`；但 GitHub 已禁 force-push/删除，防手滑覆盖另一台推上来的成果。

## 1. 每台电脑接入（各做一次）
```powershell
git lfs install
cd D:\workspace
git clone https://github.com/victor2025PH/boundless.git
cd wujie
powershell -File tools\install_hooks.ps1                                          # 装 pre-push 门禁钩子
powershell -File deploy\deploy.ps1 -Action provision                              # 只读看运行时缺口
powershell -File deploy\deploy.ps1 -Action provision -Apply -From <备份路径>       # 补 config 机密
powershell -File deploy\status.ps1 -Profile all                                   # 看本机该跑的服务
```
> 装好后**不要再保留** `模仿音色`/`telegram-mtproto-ai` 等独立老仓（已归档封存）。

## 2. 五台电脑的分工（按"机器擅长什么"分，不是分给不同人）
| 机器 | 主要用途 | 常改区域 |
|---|---|---|
| 中枢(117) | 集成 + TTS 运行 | platform/ deploy/ 集成 |
| **5090 机** | **换脸/换声开发 + 调试**（有 GPU/facefusion/模型） | **engines/avatarhub/** |
| 官网机 | 官网 | website/ |
| 获客机 | 真机 RPA | engines/huoke/ |
| GPU 算力机(176/104/140/198) | 纯运行时 | 一般不改码，pull 后跑 |
> 建议：**一台机器主攻一个区域**。这样即使你忘了同步，两台机器改的也是不同文件，几乎不会真冲突。

## 3. 日常流程（单人直推版）
```powershell
# ① 到任意一台机器，开工第一件事：拉最新
git pull --rebase origin main

#   ……在 wujie 里改代码……

# ② 收工/切换机器前：自检 → 提交 → 推
powershell -File tools\repo_doctor.ps1        # 门禁 FAIL=0（pre-push 也会自动跑）
git add <你改的路径>                            # 勿 git add -A 抓无关文件
git commit -m "feat(avatarhub): ..."
git pull --rebase origin main                 # 推前再并一次（防另一台已推）
git push origin main
```
- **分支可选**：只在做"风险大/跨多天"的实验时才开 `feat/*` 分支，做完合回 main；日常小改直接在 main 上直推即可。
- 运行时机器在相关代码更新后：`git pull` → `deploy\status.ps1` → 需要则 `deploy\up.ps1 -Only <svc> -Force` 重启。

## 4. 单人多机·防自撞
1. **一台机器同一时刻只主攻一个区域**；要在两台机器同时干活，就分到不同区域（如 5090 改 avatarhub、官网机改 website）。
2. **切机器 = 先 push**：走之前 `commit`+`push`，到另一台 `pull` 再干。别把半成品锁在某台机器里。
3. **推被拒 = 先 pull --rebase**：说明另一台已推新提交，`git pull --rebase` 合并（解冲突）后再推。
4. **多开 Cursor**：同一台开多个 Cursor 会话时，让它们改不同区域/分支，别同时动同一批文件。
5. 绝不 `git push --force`（已被 GitHub 挡）；绝不提交机密/权重。

## 5. avatarhub（换脸/换声）专项 · 5090 机
- **唯一真源 = wujie**；5090 上的旧 `模仿音色` 交接后封存。
- 换脸 **dev + debug 都在 5090**（它有 GPU/facefusion/模型）：在 5090 的 wujie 副本里改 `engines/avatarhub/`，本机 `deploy` 起服务调试。
- **交接一次性动作**：5090 老仓最后一次 commit+push → 中枢 `tools\sync_engine.ps1 -Engine avatarhub -Apply` 并入 wujie → 5090 改用 `git clone wujie` 开发。此后不再用 sync_engine（除非回捞历史）。

## 6. 门禁与卫生
- 推前门禁：`tools\repo_doctor.ps1` 必须 FAIL=0（`pre-push` 钩子已自动跑；确需绕过 `git push --no-verify`）。
- 大文件走 LFS（`.gitattributes` 已配 fonts/mp4/psd 等）。
- 每周：删已合并分支、`git gc`、查新混入的 >10MB 文件。

## 7. 场景速查
| 场景 | 做法 |
|---|---|
| 到一台机器开工 | `git pull --rebase origin main` |
| 改完要走 | doctor → `git add <改动>` → commit → `pull --rebase` → `push` |
| push 被拒(non-fast-forward) | `git pull --rebase origin main` 解冲突 → doctor → 再 push |
| 改换脸 | 5090 上按上面流程改 engines/avatarhub |
| GPU 机上新版 | `git pull` → `deploy\up.ps1 -Only <svc> -Force` |
| 忘了在哪台改过 | 各机 `git status`/`git log --oneline -5` 对一下，谁有没推的先推 |
