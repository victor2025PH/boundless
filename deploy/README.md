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

## 待办（Phase 7）
把三引擎 + 官网的"起停/健康/provision"收敛为一个全域 `deploy/up.ps1`（调用各引擎既有脚本），实现"一处拉起全栈"。
