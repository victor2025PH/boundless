# Fleet 本机冒烟脚本

- `fleet_local_smoke.ps1`：一键 init 主控(18798) + 实例 stub(18790) + enroll → heartbeat → ping/account_health/pull_overview done
- `fleet_smoke_instance_stub.py`：最小 `GET /api/ping`、`GET /api/accounts/fleet-health`、`GET /api/player-care/overview`（数字摘要，无聊天原文）

## 用法

```powershell
cd D:\boundless-fleet-p2\engines\chengjie
powershell -ExecutionPolicy Bypass -File .\scripts\fleet_local_smoke.ps1
# 保留进程便于手工再测：
powershell -ExecutionPolicy Bypass -File .\scripts\fleet_local_smoke.ps1 -KeepRunning
```

密钥落在 `D:\tmp\fleet_local_smoke\secrets.env`（及同目录 config），**禁止 git add**。
生产 / `bd2026.cc` 不在本脚本范围内。

## 阶段5 · 获客链（handoff / commandbus）

- `fleet_stage5_huoke_smoke.ps1`：跑 `test_contacts_handoff` + `test_player_care_handoff` + `test_player_care_commandbus`（tmp db，不起真 WA、不启主控）。
- 契约小节：`docs/FLEET_STAGE2_CONTRACT_SAMPLES.md` **§E**。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fleet_stage5_huoke_smoke.ps1
```
