# 实施13 · 五机运营闭环 + 四视图脚手架 + grant 软门控 + Console 运维面

> 日期：2026-07-18 ｜ 上承：实施12（人设闭环 / KPI / 商机 / .117 执行包）
> 本轮把「可执行包」推进到「可挂机运营 + 可试点接线」，并在方案基础上做了若干二次优化。

---

## 一、本轮交付（C1–C6）

| 包 | 交付 | 状态 |
|---|---|---|
| **C1 cron** | `deploy/cron/`：uploader / purge / export 的 run + install/list/uninstall；缺省 WhatIf | 包就绪；`-Engine avatarhub` WhatIf 通过 |
| **C2 定价文案** | `website/lib/bot-knowledge.ts`：挂牌统一 **USD**，USDT 仅作结算表述 | 已收口 |
| **C3 四视图** | `engines/avatarhub/product_views/`（四 yaml + `loader.py`）+ `APPLY.md` 最小接线补丁 | 脚手架自测通过；**故意未改** 3.2 万行 `avatar_hub.py` |
| **C4 grant 软门控** | `platform/identity/grant_gate.py` + `fetch_grants.py` + `POST/GET …/sync/personas/grants` + 三引擎 `grant_check` 薄封装 | 默认 **warn/放行**；`PERSONA_GRANT_ENFORCE=1` 才拒 |
| **C5 Console 运维面** | `/console/opportunities` 独立页、`/console/audit`、`/console/health`（五机 mesh）、导航与 ROADMAP 对齐 | 已接线；`tsc` 0 |
| **C6 purge 收口** | purged 后 `scrubSlotsDetailFingerprints`；purge agent 发 `platform.persona.purged` 遥测 | 三引擎 + ledger 侧齐 |

---

## 二、相对原方案的二次优化（实施中深入思考后改动的点）

1. **Grant 默认软门控，而非一步到位强制拒**  
   断网/缓存过期/未同步时仍放行并打审计日志；强制模式靠显式 env。避免「人设总线未稳就挡业务」。

2. **四视图只交脚手架 + APPLY，不热改 hub 巨文件**  
   原方案若直接改 `hub_ui`/`hub.js`，误改成本极高。改为 yaml 驱动 + ~15 行推荐补丁，试点时再手工合入某一个 `PRODUCT_ID`。

3. **Cron 包缺省 WhatIf，且强制 `-Engine`**  
   避免无参安装挂死、误装到错误机器；生产 `-Execute` 前必须配好机器级 `EVENT_INGEST_KEY`。

4. **健康页用 `machine-mesh.ts` 静态拓扑 + 本地可观测信号**  
   集团不读产品内容库；健康面只看「事件/人设同步/账本」侧可达性与时效，符合联邦边界。

5. **purged 指纹 scrub 与遥测同轮完成**  
   软删 trash 之外，ledger 侧去掉 slots_detail 可逆指纹，避免「已清除」状态仍可被侧面还原。

6. **定价文案只改 Bot 知识层挂牌币种表述**  
   SKU 数字在实施10已按竞品×2落库；本轮只消「挂牌写 USDT」的尾差，不重开定价决议。

---

## 三、集成验证（本机）

- `website`：`npx tsc --noEmit` = 0  
- `grant_gate.py --selftest`、`product_views/loader.py` selftest、三引擎 purge agent 相关改动可编译  
- `deploy/cron/install_tasks.ps1 -Engine avatarhub` WhatIf 路径通过  
- **未**执行：五机 `-Execute`、`.117` 真迁、`PERSONA_GRANT_ENFORCE=1`

---

## 四、提交边界

只提交 Boundless 联邦架构相关路径；老板在途（DragonQuest / 表单 / 其它 WIP）与 `docs/实施10_合规隔离_13xlol_*`（独立合规议题）**不纳入**本轮提交。

---

## 五、下一阶段（实施14 建议）

按价值排序：

1. **`.117` 通译双实例真机迁移**（运维按 runbook；观察期后翻 `enabled`）——最高业务价值  
2. **五机 cron `-Execute`**（每机独立 `EVENT_INGEST_KEY`；先 uploader，再 purge `--commit`）  
3. **avatarhub 四视图接线试点**：按 `APPLY.md` 合入一个 `PRODUCT_ID`，验证侧栏裁剪后再推全四产品  
4. **Grant enforce**：确认 `fetch_grants` 定时同步稳定 + 审计无误报后，再开 `PERSONA_GRANT_ENFORCE=1`  
5. **首份真实 KPI 周报**：uploader 跑通后跑 `kpi-weekly-report.mjs` 校准口径  
6. **暂缓**：统一 License 签发服务（影子账本 + 引擎签发已够用）
