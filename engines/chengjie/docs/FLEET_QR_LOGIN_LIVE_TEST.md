# 控制台集中扫码登录 — 真机测试手册（草案，未执行）

设计：`FLEET_CONSOLE_QR_LOGIN.md`。本手册里每一步涉及主控（VPS）或真机实例的操作都要 Morgan 事先批准，
按批准的范围执行；**本手册本身不授权任何生产操作**。

## 0. 先跑本地端到端（无需批准，117 上）

```powershell
cd D:\fleet-p2-fixes\engines\chengjie
python -X utf8 tools\fleet_qr_login_e2e.py --shots D:\fleet-issue-logs\qr-e2e            # 期望最后一行 "ok": true，界面「登录成功：whatsapp-e2e-1」
python -X utf8 tools\fleet_qr_login_e2e.py --scenario failed                              # 期望 "ok": true，界面「已结束：failed (e2e_failed)」
python -X utf8 -m pytest -q -p no:cacheprovider tests\test_fleet_console_qr_login.py tests\test_fleet_qr_login_e2e.py
```
harness 只用 127.0.0.1 随机端口（排除直播端口）、临时目录里的主控库和 Agent 状态目录；真实的主控路由、真实 Agent、
真实 `fleet_console.html`（只有 `base.html` 是桩）、假实例。截图在 `--shots` 目录。

## 1. 选哪台、哪个实例

| 选择 | 节点 | 实例 | 说明 |
|---|---|---|---|
| **首选** | ZHUJI-117 `n_29357c896bd9`（group lab） | `chatx` → `http://127.0.0.1:18799`（domain conversion，桌面版 telegram-ai-desktop） | 操作人就在这台上，能同时看实例界面 |
| 备选 | YUYAN-173 `n_67d0f0d5e3ca`（group lab） | `chatx` → `http://127.0.0.1:18799` | 仍是 0.2.1 且状态目录未加锁；先按 RELEASE_0.3.3 升到 0.3.3 再测 |
| **禁止** | GANZHI-176 `n_c2bb96108707`（group live，直播机） | 任何 | 直播机不做登录测试 |
| **禁止** | 任何 `role=health` 实例、任何直播端口 7910/7916/7920/8000/8080/8766/9000 | — | Agent 也会拒绝 |

平台：WhatsApp 或 Telegram，二选一。账号必须是**测试号**（不是正在接客的号），手机在操作人手里。

## 2. 前置条件（逐条确认后再开始）

1. 主控已部署含本功能的代码（`/api/fleet/nodes/{id}/login-qr` 存在）——见 `D:\fleet-prod-plan\RELEASE_0.3.3.md` §4，需批准。
   检查（运营 token 只放环境变量，不落盘）：
   `curl -s -o NUL -w "%{http_code}" -X POST -H "Authorization: Bearer $env:OP" -H "Content-Type: application/json" -d "{}" https://bd2026.cc/fleet/api/fleet/nodes/n_29357c896bd9/login-qr`
   → `400`（platform 缺失）说明端点在；`404` 说明主控还是旧版，停。
2. 节点 Agent：0.3.3 最好（有 Agent 端形状校验）；0.3.2 / 0.3.1 也能执行 `login_qr` / `login_status`。控制台节点行显示「在线」。
3. 实例在线：控制台节点行「实例」列 `1/1`；在节点本机能打开实例界面。
4. 该实例的平台登录功能开着（实例界面里能手动发起扫码登录）；先在实例界面手动扫一次确认测试号能登录，再退出登录。
5. 不在直播时段做（虽然 117 / 173 不是直播机，控制台和主控是共用的）。
6. 记录开始时间（CST），方便事后在主控 `node_tasks` 里定位本次任务。

## 3. 步骤

1. 打开 `https://bd2026.cc/fleet/console`（用运营账号登录）。
2. 节点表找到 ZHUJI-117，点「扫码登录」。下方出现「扫码登录」面板。
3. 实例选 `chatx`（不要选「自动」，以免选到别的实例）；平台填 `whatsapp`（或 `telegram`）。
4. 点「生成二维码」。
   - 期望：几秒内文字变「已下发，等节点取回二维码…」→ 出现二维码 →「请用手机扫码」。
   - 若 60 秒仍无二维码：看「最近任务」里的 `login_qr` 状态（`queued` = 节点没来领；`failed` = 实例拒绝，看结果里的 detail / reason_code）。
5. 用测试手机扫码（WhatsApp：设置 → 已关联设备 → 关联新设备；Telegram：设置 → 设备 → 扫码）。
   - 期望：二维码过期前完成；页面每 4 秒查一次，文字「等待扫码… 状态 pending」；二维码若被实例刷新会自动替换。
6. 手机确认后，期望页面显示「登录成功：<account_id>」，二维码消失，「生成二维码」可再点。
7. 核对：
   - 节点本机实例界面能看到新登录的测试号；
   - 控制台「最近任务」：1 个 `login_qr` done（detail `qr_ready`），若干 `login_status` done，最后一个结果 `status=authorized`；
   - 任务列表里没有二维码图片（只有「查看结果」点进单个任务才有）；
   - 节点下一次心跳的账号数 +1。
8. 负面检查（同一面板，各做一次）：
   - 点「停止」中断一次进行中的流程 → 文字「已停止」，二维码消失，还在排队的任务变 `cancelled`；
   - 平台填 `../x` → 页面直接提示平台名不对，不下发任务；
   - 两个浏览器标签同时对同一个 login_id 轮询时，主控不会叠加排队的 `login_status`（同一个任务被复用）。

## 4. 期望结果汇总

| 项 | 期望 |
|---|---|
| 首次出码 | ≤ 10 秒（节点长轮询 + 实例起会话） |
| 登录成功 | 扫码确认后 ≤ 8 秒页面显示「登录成功」 |
| 失败 / 过期 | 页面显示「已结束：failed / expired (原因码)」，二维码消失 |
| 5 分钟没扫 | 页面「5 分钟内没有完成扫码，已停止」 |
| 任务量 | 每次登录 1 个 `login_qr` + 每 4 秒最多 1 个 `login_status` |

## 5. 回滚 / 收尾

- 只是测试登录：在节点本机实例界面把测试号退出 / 删除（这是实例里的账号操作，需要操作人确认）。
  进行中的会话可在本机调用实例的 `/api/platforms/{p}/login/{login_id}/cancel`，或等它自己过期。
- 控制台上：点「停止」或「收起」会取消还在排队的任务；已完成的任务记录保留在主控库，不需要删。
- 功能有问题需要撤回：主控回滚到上一版代码（见 RELEASE_0.3.3.md §5 主控回滚），旧控制台仍有原来的 `login_qr` 任务按钮；
  Agent 不需要回滚（新旧 Agent 都兼容旧主控）。
- 记录：把开始/结束时间、节点、实例、平台、结果、截图放进 `D:\fleet-issue-logs\qr-live-<日期>\`。

## 6. 已知限制

- 需要密码 / 短信验证码 / 2FA 的登录方式不能在控制台完成，会停在 pending / failed，请在节点本机完成。
- 0.3.1 / 0.3.2 Agent 不做平台名 / login_id 的形状校验（主控已校验）；也不会把 `reason_code` 回传到 `login_status` 结果里。
