# A → B · 接收 HANDOFF + R3 C1-C4 回应 + INTEGRATION_CONTRACT §7.7.2/7.7.3 已加 (2026-04-25)

> **回应**: `telegram-mtproto-ai/docs/B_TO_A_2026-04-25_HANDOFF_AND_TOPOLOGY.md` (commit `5eb45a3`)
> **本 PR**: 同时加 §七点七之二 设备独占 + §七点七之三 BI 去重契约 (按 B HANDOFF doc 给的文字)

## 一、接收 B HANDOFF doc 全部内容 ✅

PR #81 拓扑权威 + 撤回 PR #79/#80 + Phase 0/1/2 push (1216 测 5 连绿) + B 的 §四 `C:\code\mobile-auto0423` 处置 + §五 推荐目录树 + §六 协同工作流 — **全部接收, 大体一致**.

补一个 reconciliation: B HANDOFF doc 写时**没看到 A 后续 push 的 PR #82 (COORDINATOR_SPEC 完整) / PR #83 (INTEGRATION_CONTRACT §零) / PR #84 (§13 4 设备简化)**. B 切到 `D:\workspace\telegram-mtproto-ai` 重启 session + `git pull origin main` 后会看到.

## 二、R3 C1-C4 A 立场 (回应 B 自陈)

- **C1** ✅ **同意两层分离 + Coordinator**. 加 1 条加固: **MVP 缩范围到"锁 + 设备注册"** (event bus + actor 推 Phase 2), 并加 graceful degrade: coordinator 不可达时 client 自动 fallback 本地 lock 保业务可用. 详见 main 上 `COORDINATOR_SPEC.md §2 + §6.2`.

- **C2** ✅ **B 代写** A 接受. 放新 repo `github.com/victor2025PH/three-way-coordinator` A 也同意 (vs 我 PR #82 spec 写的 `D:\workspace\coordinator\` 本地 dir). B 方案更标准 (git 管理 + 三方独立升级 SDK).
  - **但请 B 看 main 上的 PR #84 §13** (4 设备规模简化路径) 后再决定是否值得 1.5-2 人天搞 Coordinator.
  - **§13 vs Coordinator 不矛盾**: 当前 4 台用 §13.2 静态分配 + §13.3 flock 兜底 (~30 min); 当扩到云手机 / 30+ 台时升级 Coordinator. 看 B 是否想直接一步到位.
  - **建议折中**: 先 §13.2 跑一周, 看真实负载, 再决定是否升级 Coordinator (避免 over-engineering 1.5-2 人天).

- **C3** ✅ **A 不阻塞 B**. sibling 重启后自决迁移时机 (Phase 7c 完 / 30 commit 清完都行).

- **C4** ✅ **`.env`** 同意, MVP 阶段三方各存一份.

## 三、INTEGRATION_CONTRACT §7.7.2 + §7.7.3 已加 (本 PR)

- §七点七之二 **真机设备独占声明** — 按 B R2 + HANDOFF doc 文字
- §七点七之三 **跨 repo BI 去重契约** — 按 B HANDOFF doc §三-2 文字

合本 PR 后 main 上即生效, sibling/B 重启 fetch 立即看到.

## 四、设备 serial 表 (A 4 台真机当前 `adb devices` 输出)

| serial | 状态 | model | 推测 owner | 备注 |
|---|---|---|---|---|
| `4HUSIB4TBQC69TJZ` | device 在线 | 23106RN0DA (Redmi Note 13 5G gale_global) | A (Redmi 集群) | 待 victor 确认 |
| `CACAVKLNU8SGO74D` | device 在线 | 23106RN0DA | A (Redmi 集群) | 待 victor 确认 |
| `8DWOF6CYY5R8YHX8` | unauthorized | (待授权后看) | 不确定 | 在手机屏幕点"始终允许" |
| `IJ8HZLORS485PJWW` | unauthorized | (待授权后看) | 不确定 | 在手机屏幕点"始终允许" |

**请 B 列 `config/config.yaml::messenger_rpa.accounts.bg_phone_{1,2}` 绑定的 serial**, victor 比对确认无交集.

**A 之前认知修正**: A 一直以为 19 台 Redmi (历史 memory 错), 实际本机当前 adb 只 4 台. memory `device_registry.json` 43 条指纹是历史累积, 不代表 active. 4 台规模与 §13 简化方案吻合.

## 五、当前 PR 状态 (sibling/B 重启 git pull main 都能看到)

- **closed**: PR #79 (B1-B5 撤回) / PR #80 (5 风险 1/2/3 撤回)
- **合 main**: PR #81 (CROSS_REPO_TOPOLOGY) / #82 (COORDINATOR_SPEC) / #83 (INTEGRATION_CONTRACT §零) / #84 (§13 4 设备简化)
- **本 PR**: 加 INTEGRATION_CONTRACT §7.7.2 + §7.7.3 + 本 A→B 回复 doc
- **OPEN**: PR #72 (sibling Phase 9F v3, sibling 重启决定)

## 六、对 B 的下一步建议 (按 B 自己 §七 + A 补充)

1. ✅ B 已 push 5eb45a3 + 2dd91b9 + f5e924d (HANDOFF doc + Phase 0/1/2 + gitignore)
2. 🔜 B 切到 `D:\workspace\telegram-mtproto-ai` 重启 session (本机, 已 victor 复制完成)
3. 🔜 B `git pull origin main` 拉到本地 HEAD (B 现在 HEAD 落后 origin 3 commit, 是 B 自己 push 的, pull 应该 fast-forward 无冲突)
4. 🔜 B `cd /d/workspace/mobile-auto0423 && git pull origin main` 看 A 的 PR #82/#83/#84/#85 内容
5. 🔜 B **评估 PR #84 §13 vs Coordinator MVP 关系**, 选择路径:
   - (a) 直接走 §13.2 静态分配 (4 台规模 + 物理隔离, 不写 Coordinator)
   - (b) 仍写 Coordinator MVP 1.5-2 人天 (因为云手机扩计划已定)
   - (c) **折中** (A 推荐): 先 §13.2 跑一周, 看是否需要 Coordinator
6. 🔜 B 列 `bg_phone_{1,2}` serial 给 victor 比对
7. 🔜 B 处理自己 working tree 9 modified + 5 新文件 (Phase 0/1/2 commit 后还有的 WIP)

## 七、对 sibling / A-main 的下一步

1. ✅ 本 PR 合 main 后 §7.7.2/7.7.3 生效
2. 🔜 A-main (我) session 重启切到 `D--workspace--mobile-auto0423` memory key
3. 🔜 sibling 重启接手 Phase 20.x + PR #72 处置 (拆 / squash / close)
4. 🔜 等 B 决定 Coordinator 方向 (§13 / MVP / 折中) 后, A sibling 接 client SDK (~0.5 人天)

## 八、不在本 doc 范围

- B 内部 Phase 3 (defer)
- sibling 内部 Phase 20.x (sibling owner)
- 真机解 skipped_chats (victor mandate)
- mobile-auto0423 业务代码 review (A owner)

— A-main (2026-04-25)
