# deploy/ · 全域部署索引（多引擎 + 多机拓扑）

> 提层原则：**索引而非搬迁**。各引擎的部署资产随引擎走（改路径会破坏其相对假设）；本层只做全域索引 + 跨引擎拓扑单一源，便于一处总览、逐机 provision。

## 各引擎部署资产（就地）
| 引擎 | 部署资产 | 说明 |
|---|---|---|
| avatarhub | `engines/avatarhub/docker/`（compose + Dockerfile.service）· `provision.*` · `requirements/`（各 conda env 基线）· `installer/` | 微服务多机；GPU 池见 docker-compose |
| chengjie | `engines/chengjie/docker/`（含 `docker-compose.ha.yml`）· `requirements.txt`/`-ci.txt` | 单进程 + SQLite（文档注明不可多进程写） |
| huoke | `engines/huoke/`（`server.py` + `migrations/` + `requirements.txt`） | 主控 + Worker 集群 |
| website | `website/`（Next.js；`_server-yuntech` deploy.ps1 Posh-SSH 到 usdt2026） | 部署前 `npm run build` |

## 跨机拓扑（单一源）
`deploy/cluster_map.json`（从 avatarhub 复制的拓扑单一源）：机器 IP × 服务 × 显存预算。
> 与 `engines/avatarhub/env_config.bat` 的 `SVC_*` 路由由 `engines/avatarhub/tools/topology_lint.py` 巡检一致性；迁移/换机只改拓扑这一处。

## 第三方引擎（不入库，provision）
`vendor/`（index-tts 等）由 provision 部署；见各引擎 `requirements/` 与 `MANIFEST`。

## 全域起停/健康/provision（Phase 7 · 已落地）

一处拉起全栈，委派各引擎**既有脚本**（不重造轮子），端口幂等，三态健康。

| 命令 | 作用 |
|---|---|
| `powershell -File deploy\status.ps1` | 只读健康（GO/DEGRADED/DOWN），退出码 0/1/2；`-Profile all -Json` 供监控 |
| `powershell -File deploy\up.ps1` | 幂等拉起 core+web（端口在听则跳过）；`-Only huoke -WhatIf` 干跑；GPU 服务需 `-Only`/`-Force` |
| `powershell -File deploy\down.ps1 -Only website -Force` | 停服务（默认只提示，`-Force` 才真停，`-WhatIf` 预览） |
| `powershell -File deploy\deploy.ps1 -Action provision` | 只读报运行时缺口；`-Apply -From <备份根>` 从备份补 `config/*.yaml` |

- **单一源 `deploy/stack.json`**：逐服务登记 dir/runtime/entry/ports/health/委派脚本/剖面/provision 依赖。改起停方式只改这一处。
- **剖面 profiles**：`core`(chengjie+huoke，任意机、无 GPU) · `web`(官网) · `gpu`(avatarhub 全栈 + TTS/STT + index-tts，按 `cluster_map.json` 分机)。
- **与 cluster_map 分工**：`stack.json`=本机『逻辑服务怎么起停/探活』；`cluster_map.json`=跨机『IP×服务×显存预算』。换机改 cluster_map，改起停改 stack.json。
- **委派而非重造**：huoke 直接用其成熟 `start/stop/status(-Json)`；chengjie 用 `start_main.ps1`；avatarhub 用 `boot_stack.bat`；index-tts 以正确 cwd 直起 uv（绕开已过期路径的 bat）。

### 运行时迁移（让引擎真从 wujie 起）
引擎是"拉净码"迁入，**运行时不在 git**（机密/模型/登录态）。先体检缺口，补齐后再起：
```powershell
powershell -File deploy\deploy.ps1 -Action provision -Profile core          # 看缺什么
powershell -File deploy\deploy.ps1 -Action provision -Apply -From "D:\workspace\_workspace_backup_20260715"  # 从备份补 config/*.yaml
# 另需：各引擎 pip 依赖(建议独立 venv)、chengjie sessions/(登录态)、index-tts uv sync+权重
powershell -File deploy\up.ps1 -Profile core                                 # 补齐后拉起
powershell -File deploy\status.ps1 -Profile all                              # 复核
```
