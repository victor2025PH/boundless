# boundless-hud — 集群实况壁纸 HUD（六机套件）+ 算力模式 SSOT

> P0 收编 git（2026-08-10）。此前整套代码只活在 `C:\Users\Public\boundless-hud\` 与
> `tools\` 的未跟踪副本里；本目录是 **git 单一真相**（117 侧副本有实验残留，以中枢机为准源收编）。

## 这套东西是什么

每台机的桌面壁纸就是集群仪表盘：

- **sentinel.ps1** — 每分钟一跳的六态状态机（netdown / gpufault / ipdrift / hubdown /
  svcdown / normal），跑在交互会话的计划任务 `BoundlessHudSentinel` 里（wscript 隐窗启动）。
  normal 态：中枢机现渲实况板，节点机从中枢 `:7912` 拉本机视角板（拉不到 200 就回落静态
  身份壁纸——新鲜度判决在服务端）。v5 起自带 telemetry_agent 看门狗。
- **telemetry_agent.ps1** — 秒级 GPU 上报（nvidia-smi --loop 常驻子进程 → 中枢 `:7913
  /api/telemetry`），由哨兵每分钟拉活，pid 文件防双开。
- **register_task.ps1** — 在本机注册哨兵分钟任务（wscript run_hidden.vbs 隐窗壳 +
  3 分钟执行上限防 nvidia-smi 卡死冻结整板）。
- **run_hidden.vbs** — 零闪窗启动壳（交互任务直指 powershell.exe 每分钟弹黑框的解法）。
- **modes.json** — **集群算力模式 SSOT**（chatx 智聊 ⇄ face 换脸/直播 ⇄ code 编程，
  2026-08-14 起互斥三模式）：每模式的执行步骤（引擎起停 / Ollama 钉卸 / 远端泊车唤醒 /
  WSL vLLM 起停 / NLLB 卸载回载）、每机角色 chip、zh/accent（HUD/壁纸/控制台配色文案
  全数据驱动，增删模式零改前端）、哨兵 watch_ports 覆盖、不变量（绝不卸载清单）。详见文件头注。
- **deploy_hud.ps1** — 六机部署：（可选 `-GitPull` 让有仓的节点先 `git pull`）→ 覆盖
  `C:\Users\Public\boundless-hud\` → 按台账+当前模式生成 config.json → 重注册任务。

## 留在 tools\ 的两件（也已入 git，勿移动）

| 文件 | 为什么不搬进本目录 |
|---|---|
| `tools/render_cluster_board.py` | 实况板渲染器（仅中枢跑，`board_cmd` 指它）。活体 HUD 的 `tools/hud/hud_contract_test.py` 以**文件路径+文本锚**对它做令牌同源断言（INK_*/NEON_* 必须出现在 hud.html），`tools/hud/hud_server.py`（:7913，另一条在建工作线）import 判据同源——搬家=掰断别人在跑的活。 |
| `tools/xiaojie/xiaojie_server.py` | `:7912` 板服务器 + 小界值班 AI（任务 `BoundlessXiaojieServer`）。同上：`hud_server.py` 直接 `import xiaojie_server`。 |

壁纸模板生成器 `tools/make_machine_wallpapers.py`（模板底图 → `brand-assets/05_backgrounds/machines/`）
本就在 git，产物 PNG 随本次一并入库（部署脚本的壁纸源，缺了 git pull 部署链就断）。

## 算力模式切换（chatx ⇄ face ⇄ code 互斥三模式）

执行器挂在 Hub（`engines/avatarhub` 的 `hub_routes/cluster_mode.py`）：

```
GET  http://192.168.0.176:9000/api/cluster/mode          # 当前模式/进度/冷却（?gate=1 带闸门预判）
POST http://192.168.0.176:9000/api/cluster/mode          # {target, dry_run, force, reason}
python tools\cluster_mode.py --status|--dry-run face|--switch face --reason "..."   # avatarhub 仓 CLI
```

- **闸门**（服务端判定）：hub_busy ≠ safe / 同传会话在跑 / 出片队列非空 / 冷却 <10min → 拒切。
- **失败自动回滚**上一 profile；事件流 `logs/cluster_mode_events.jsonl`（壁纸板直接消费，
  开始/每步/完成/回滚有始有终）；回滚失败 raise_alert 常驻到人工处理。
- **显存账本联动**：`engines/avatarhub cluster_map.json` 的 `vram_budget.mode_services`
  分模式记账，`tools/topology_lint.py` 分模式验收 + 与本目录 modes.json 对账（budget_key
  必须有账、模式名集合必须一致）。
- **不变量**（绝不卸载）：140 bge-m3（智聊嵌入主路）、173 qwen14b-fallback（断云兜底）、
  117 CosyVoice3+Qwen3TTS、104 faceswap——modes.json invariants + 执行器双重硬拦。

## 部署

```powershell
# 全量六机（在中枢机跑；-Only tingxie 只发一台；-GitPull 让有仓节点先拉再覆盖）
powershell -ExecutionPolicy Bypass -File D:\boundless\deploy\boundless-hud\deploy_hud.ps1
```

哨兵/遥测/注册器改动 → 改本目录 → 重跑部署（`tools/deploy_hud.ps1` 已改为转发壳）。
渲染器/小界改动 → 原地改 `tools/` 下文件（中枢本机生效，无需分发）。
