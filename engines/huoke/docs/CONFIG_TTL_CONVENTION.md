# 配置临时改动 TTL 约定 — Phase 2 P1.5

> **元工程防护** — 防 4-21 VPN gate / 4-26 `enabled_probability` 同类事故复发.
>
> 历史教训: yaml 临时改动潜伏 6 天 → 5h 死循环事故. 见 `memory/session_handoff_2026-04-27_pr119_circle_prospect_hotfix.md`.

## 约定

任何**临时**改动 yaml 配置 (实战联调 / 演示需要 / 紧急放行) 时, 必须在
改动行的紧邻**上方**加 TEMPORARY 注释, 写明:

1. 复原日期 (`YYYY-MM-DD` 格式, 一般 7-14 天)
2. 一句话原因 (含原值, 方便复原)

```yaml
# TEMPORARY until 2026-05-15: 联调 v2rayNG 路由测试, 原值 enforce_preflight=true
manual_gate:
  enforce_preflight: false
```

## 自动检查

`scripts/ops/check_temp_configs.ps1` 扫所有 `config/**/*.yaml` 找该模式,
比对今天日期, 输出:

| 状态 | 含义 |
|------|------|
| `[OK +Nd]` | 距过期还有 N 天, 安全 |
| `[DUE in Nd]` | 距过期 ≤ 7 天, 提醒处理 (黄色) |
| `[EXPIRED -Nd]` | 已过期 N 天, 必须复原 (红色) |

退出码: 0=OK, 1=有 due, 2=有 expired (适合 cron / monitor 消费).

`status.bat` 启动时也会自动调用 (informational, 不影响 verdict).

## 命令速查

```bash
# 全扫
scripts\ops\check_temp_configs.ps1

# JSON 输出 (cron / 自动化用)
scripts\ops\check_temp_configs.ps1 -Json

# 提前 14 天提醒 (默认 7 天)
scripts\ops\check_temp_configs.ps1 -DaysAhead 14

# 扫别的目录 (e.g. 自定义子目录)
scripts\ops\check_temp_configs.ps1 -Path config\experimental
```

## 反例 — 不应该这样写

```yaml
# 不要: 没有日期, 永远不过期, 等于无效防护
# TEMPORARY: 临时关闭

# 不要: 日期格式错 (扫描器只认 YYYY-MM-DD)
# TEMPORARY until 5/15/26

# 不要: 写在改动行**下方** (扫描器不要求, 但人眼跟改动关联弱)
manual_gate:
  enforce_preflight: false
  # TEMPORARY until 2026-05-15  ← 反模式
```

## 复原工作流

过期警告出现时:

1. `scripts\ops\check_temp_configs.ps1` 看完整列表
2. 对每条 expired:
   - 如仍需保留: 改 `until` 日期 + 在 commit 说明展期理由
   - 如可复原: 改回原值 + 删 TEMPORARY 注释
3. commit + push, 不要无声忽略警告

## 历史事故对照

| 日期 | 改动 | 是否有 TTL | 后果 |
|------|------|-----------|------|
| 2026-04-21 | `enforce_preflight: false` | ❌ 无 | 潜伏 6 天 → 4-26 社群客服拓展 5h 死循环 |
| 2026-04-26 | `enabled_probability: 1.0` (b2b self test) | ❌ 无 | 仍是隐患 (用户记忆里) |
| 2026-04-20 | `gate_mode: dev` | ✅ 当天复原 | 无事故 |

**结论**: 当天即复原的临时改动可不加 TTL; 跨天的必须加.
