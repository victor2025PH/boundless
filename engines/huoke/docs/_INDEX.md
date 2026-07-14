# docs/ — 文档导览

> 本文件是 docs/ 目录的索引。SSOT 是 [SYSTEM_RUNBOOK.md](SYSTEM_RUNBOOK.md)。

## 顶层（高频运维入口）

| 文件 | 用途 | 受众 |
|------|------|------|
| [SYSTEM_RUNBOOK.md](SYSTEM_RUNBOOK.md) | ⭐ 运维 SSOT：启停/诊断/F1-F8 故障字典/Claude onboarding | 所有人 |
| [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md) | 进程拓扑+任务派发时序+数据层+端口 | 开发 |
| [CAPABILITIES.md](CAPABILITIES.md) | 业务能力矩阵+KPI（部分 TODO） | 市场 |
| [INTEGRATION_CONTRACT.md](INTEGRATION_CONTRACT.md) | A/B 边界权威契约（跨边界改动必先改本文件） | 所有人 |

## runbook/ — 上线/运维手册

| 文件 | 用途 |
|------|------|
| [B_OPERATIONS_GUIDE.md](runbook/B_OPERATIONS_GUIDE.md) | B worker 详细运维（已被 SYSTEM_RUNBOOK 部分覆盖） |
| [B_PRODUCTION_READINESS.md](runbook/B_PRODUCTION_READINESS.md) | B 上线前自检清单 |
| [B_RESUME_2026-04-23-EVENING.md](runbook/B_RESUME_2026-04-23-EVENING.md) | B Claude session 历史崩溃恢复脚本（参考用） |
| [PHASE10_ACTIVATION_RUNBOOK.md](runbook/PHASE10_ACTIVATION_RUNBOOK.md) | Phase 10 启用流程 |
| [PHASE10_PARTIAL_SMOKE_2026-04-24.md](runbook/PHASE10_PARTIAL_SMOKE_2026-04-24.md) | Phase 10 局部冒烟报告 |
| [L2_DEPLOYMENT_RUNBOOK.md](runbook/L2_DEPLOYMENT_RUNBOOK.md) | L2 中央客户画像部署流程 |
| [L2_OPS_HANDBOOK.md](runbook/L2_OPS_HANDBOOK.md) | L2 中央存储日常运维 |
| [DEPLOYMENT_30_DEVICE_TEST.md](runbook/DEPLOYMENT_30_DEVICE_TEST.md) | 30 台设备压测部署 |

## dev/ — 开发文档

| 文件 | 用途 |
|------|------|
| [FB_PHASE3_PLAN.md](dev/FB_PHASE3_PLAN.md) | FB Phase 3 设计 |
| [FB_PHASE5_LEAD_MESH.md](dev/FB_PHASE5_LEAD_MESH.md) | FB Phase 5 lead mesh |
| [FB_GREETING_A2_PLAN.md](dev/FB_GREETING_A2_PLAN.md) | FB 打招呼 A2 设计 |
| [MESSENGER_WORKFLOW_GUIDE.md](dev/MESSENGER_WORKFLOW_GUIDE.md) | Messenger 工作流详解 |
| [INTENT_VOCABULARY.md](dev/INTENT_VOCABULARY.md) | 意图分类词表 |
| [A_AUDIT_LOGS_SCHEMA_DRIFT.md](dev/A_AUDIT_LOGS_SCHEMA_DRIFT.md) | A 审计日志 schema 漂移说明 |
| [A_NEXT_PHASE7C.md](dev/A_NEXT_PHASE7C.md) | A 下阶段 Phase 7C |
| [B_NEXT_STEPS_2026-04-23.md](dev/B_NEXT_STEPS_2026-04-23.md) | B 下阶段任务（历史） |
| [UNMERGED_A_BRANCH_AUDIT_2026-04-24.md](dev/UNMERGED_A_BRANCH_AUDIT_2026-04-24.md) | A 未合并分支审计 |
| [FOR_MESSENGER_BOT_CLAUDE.md](dev/FOR_MESSENGER_BOT_CLAUDE.md) | 给 B Claude 的 onboarding |
| [COORDINATOR_SPEC.md](dev/COORDINATOR_SPEC.md) | Coordinator 协议规范 |
| [L2_REFERRAL_TRIGGER_DESIGN.md](dev/L2_REFERRAL_TRIGGER_DESIGN.md) | L2 referral trigger 设计 |
| [PHASE20_DEVELOPMENT_REPORT.md](dev/PHASE20_DEVELOPMENT_REPORT.md) | Phase 20 开发报告 |
| [TIKTOK_LEAD_GEN_PLAN.md](dev/TIKTOK_LEAD_GEN_PLAN.md) | TikTok 获客方案 |

## archive/ — 过期 plan（参考用）

| 文件 | 用途 |
|------|------|
| [P4-PLAN.md](archive/P4-PLAN.md) | P4 阶段规划 |
| [P6-EXPANSION-PLAN.md](archive/P6-EXPANSION-PLAN.md) | P6 扩张规划 |
| [P7-DEPLOYMENT-PLAN.md](archive/P7-DEPLOYMENT-PLAN.md) | P7 部署规划 |
| [P7-EXECUTION-PLAN.md](archive/P7-EXECUTION-PLAN.md) | P7 执行规划 |
| [P8-OPTIMIZATION-PLAN.md](archive/P8-OPTIMIZATION-PLAN.md) | P8 优化规划 |
| [P9-THREE-VIEW-OPTIMIZATION.md](archive/P9-THREE-VIEW-OPTIMIZATION.md) | P9 三视图优化 |
| [DEVELOPMENT_PLAN.md](archive/DEVELOPMENT_PLAN.md) | 早期总体开发规划 |

## 抖音/ — 抖音相关文档

中文文档子目录，TikTok 中文版工作流。

## _archive_three_role_2026-04-25/ — 三角色废弃文档

2026-04-25 拓扑澄清后归档。

---

> 文档维护规则：新增/移动文档需更新本索引。旧的"PX-PLAN"类规划完成后归 archive/。
