# 六机 SSH 台账（v2 · 功能命名）

> 更新：2026-08-05（六机按现役功能全员改名；旧名保留为过渡别名至 2026-11-05）  
> 单一源：`deploy/machines.json`（改机器只改它，然后跑下面的生成/下发/对账三件套）  
> 生成 SSH 配置：`tools/render_ssh_config.ps1` → `deploy/ssh_config.boundless`  
> 下发合并：`tools/remote_merge_ssh_config.ps1`（六机 `~/.ssh/config` 标记块内整体替换）  
> 影子清理：`tools/cleanup_legacy_ssh_hosts.ps1`（删标记块外重名 Host——声备机曾被 7 月旧块抢先匹配）  
> 对账门禁：`python tools/machines_lint.py --remote`（台账↔cluster_map↔生成产物↔本机/六机实配↔壁纸 全对账）

## 分配（角色权威源 = engines/avatarhub `cluster_map.json`，本表是其人话摘要）

| 中文名 | 主别名 | 职能别名 | IP | 账号 | GPU/屏 | 现役工作 | 旧名（过渡） |
|---|---|---|---|---|---|---|---|
| **中枢** | `zhongshu` | `hub176` `swap176` `interp176` `gpu176` | 192.168.0.176 | user | 5090·32G | Hub :9000 + 换脸主引擎 :8000/:8003 + 同传 :7900 + 渲染链 + 方言ASR + 智播控场 | huansheng, pc5090 |
| **韵声** | `yunsheng` | `tts173` `llm173` `gpu173` | 192.168.0.173 | admin | 5090·32G·4K | 质量轨 TTS（IndexTTS-2 :7865 + CosyVoice3 :7852）+ LLM 容灾 :11434 | huanying, voice173, livex173 |
| **声备** | `shengbei` | `tts117` `seat117` `gpu117` | 192.168.0.117 | Administrator | 3060·12G | qwen3_tts :7858（现役）+ CosyVoice3 热备 :7852 + 智聊坐席开发 | tongyi, hub117, pc117 |
| **脸备** | `lianbei` | `face104` `gpu104` | 192.168.0.104 | Administrator | 4070·12G | 换脸热备 :8000（08-02 主引擎收编中枢，本机保温回退位） | huanyan-node, huanyan |
| **听写** | `tingxie` | `stt140` `gpu140` | 192.168.0.140 | Administrator | 4070·12G·16:10 | STT 专机（whisper :7854 + nemo :7857） | tongchuan-node, tongchuan |
| **口型** | `kouxing` | `lipsync198` `gpu198` | 192.168.0.198 | Administrator | 4070·12G | 口型热备 :8090（中枢溢出位）+ 获客开发 | zhituo |

官网 VPS：`vps-bd2026` → `ubuntu@165.154.233.121`（`hualing_deploy`）

命名法：**主别名=功能拼音**（跟职能走，职能迁移时改台账+重发）；`<role><尾号>` 职能别名给肌肉记忆；
`gpu<尾号>` 全员统一兜底。旧名 2026-11-05 日落删除（render 脚本 legacy 段）。

## 桌面体系（壁纸 2.0 + 哨兵 v2 + 实况板 v2）

- 生成：`python tools/make_machine_wallpapers.py`（读台账，按各机原生分辨率出 ops/showcase 双版 + **5 张**警告态模板）
- 部署：`powershell -File tools\deploy_hud.ps1`（六机 `C:\Users\Public\boundless-hud\`，注册计划任务 `BoundlessHudSentinel` 每分钟）
- 哨兵 `tools/hud/sentinel.ps1` 五态：断网(红)/GPU异常(深红)/**IP漂移(紫,实际IP≠台账IP)**/中枢不可达(琥珀)/
  算力服务异常(橙)，自动换壁纸并盖 ASCII 详情戳，恢复自动换回；每轮并写 `status.json` 自报（state+VRAM）供中枢板收取
- 中枢专属：正常态壁纸=`tools/render_cluster_board.py` **v2** 每分钟渲染的集群实况板：六机 TCP 直探 + 各机哨兵自报
  （VRAM 水位条/漂移/GPU 故障）+ 推流时长 + Hub 忙态三档同源 `/api/ops/hub_busy`
- **告警推送**：实况板做唯一告警源（节点失联/服务掉线/自检告警 → avatarhub `alerts.py`，走
  `secrets/alert_webhooks.txt` 已配置的通道；去抖/退避/恢复通知全复用；刻意不往六机撒 webhook 密钥）
- 锁屏：`tools/deploy_lockscreen.ps1`（六机锁屏=showcase 版，无 IP/账号暴露）
- 上镜模式：改该机 `boundless-hud\config.json` 的 `"mode":"showcase"`（无 IP/账号版壁纸，屏幕分享/接待用）
- IP 漂移根治：路由器静态租约操作单（六机 MAC 已采）见 `docs/静态租约_路由器操作单_20260806.md`

## 键鼠网格（Deskflow 软 KVM · TLS）

- 范围 v1：**只纳入有人打字的三台**（中枢=服务端 · 声备/口型=客户端），纯算力机刻意排除防直播误滑；
  部署 `powershell -File tools\deploy_deskflow.ps1`（TLS 默认开，每机专属证书；回退 `-NoTls`）
- 证书缓存：`C:\Users\user\.ssh\deskflow_certs\`（**不可再生成换新**——客户端 TOFU 钉住服务端指纹，换证书=全员失联）
- 调试铁律：**勿经 SSH 前台跑 deskflow-core 调试**——SSH 会话的 profile 与桌面会话不同，会读写另一份指纹库，
  制造"fingerprint does not match"假故障（2026-08-06 口型机实锤半小时）；看连接状态用 `netstat -an | findstr 24800`
- 布局：声备在中枢右缘、口型在左缘（改 `deskflow-server.conf` 的 links 段后重跑部署）

## 算力互调

- Hub：`http://192.168.0.176:9000`（中枢机）
- 拓扑/显存预算：engines/avatarhub `cluster_map.json`（唯一权威）；本仓 `deploy/cluster_map.json` 只是镜像（lint G 盯漂移）
- 业务调用一律经 Hub HTTP + service token；**禁止对直播/推流机开 RDP**（RDP 抢占控制台会话，OBS/vcam/桌面捕获当场黑屏；
  要看远端屏幕用镜像类方案——Sunshine/Moonlight，见 CLUSTER_OPS）

## 演习与巡检记录

- **2026-08-06 01:12 实弹演习（脸备机受控重启）全链路通过**：失联检测+推送 30 秒（01:13:09 raise）→
  重启窗口 2 分钟 → 恢复报平安（01:15:03 resolve）→ FaceSwap_Boot 自拉 ✓ → 哨兵随自动登录自愈 ✓。
  演习期间真实直播/同传会话在跑，零影响（选热备机演习的意义所在）。
- 哨兵加固（同日）：nvidia-smi 查询 6s 硬超时（曾卡死整条哨兵 10 分钟）+ 计划任务 3 分钟执行上限
  + IgnoreNew 防并发；实况板推流徽章改与 hub_busy danger 同源（/health 的 vcam_streaming 是服务态，
  空闲也 True，曾误亮"推流中"）。
- 台账巡检：计划任务 `BoundlessMachinesPatrol` 每日 09:30 跑 `machines_patrol.py`
  （machines_lint --remote --strict，红灯推送、恢复报平安）。
- Sunshine/Moonlight 看屏（PoC 结论 2026-08-06）：宿主侧全自动可行（MSI 静默 + `--creds` +
  `POST /api/pin`，口型机已装并配对成功=named_certs 有 zhongshu）；moonlight-qt CLI 是 GUI 混合体，
  无头编排易丢客户端状态——铺开时宿主侧跑脚本，客户端在 Moonlight 窗口点一次主机图标即可（PIN 由脚本自动提交）。
  客户端便携版在中枢 `C:\Users\user\Apps\Moonlight`；凭据 `C:\Users\user\.ssh\sunshine_creds.txt`。

## 小界 · 桌面值班 AI（2026-08-06 上线）

- **形态**：六机桌面各驻一个机器人小窗（官网 EveBot 形象的运维版），Edge/Chrome `--app` 零安装拉起
  （任务 `BoundlessXiaojie`，登录自启；脸备机无 Edge 走 Chrome 回落链）
- **后端**：`tools/xiaojie/xiaojie_server.py`（仅中枢 :7912，纯标准库；任务 `BoundlessXiaojieServer`）；
  密钥只留中枢（deepseek key 读环境变量/secrets.bat，与 Hub 同机制）
- **能力**：聊天诊断（集群状态/告警/能不能重启 Hub，**判据与实况板同源**：history/alerts_state/hub_busy）、
  运维全息播报（头顶滚动六机实况）、语音回答（fish_tts 默认音色，🔊/🔇 可静音，localStorage 记忆）
- **纪律**：只读顾问不执行操作；LLM 链 deepseek→deepseek-pro→qwen14b-173(.173 在编容灾位)，
  **绝不用中枢本机 ollama**（2026-08-06 02:45 实锤：回落到 127.0.0.1 冷载 14B 钉 9.5G 挤渲染链，已立禁令）；
  直播中（hub_busy=danger）自动勿扰：不出声、机器人转安静态
- 面板 v3 同日上线：官网 tokens（ink+neon）、渐变描边、辉光 LED、VRAM 渐变条、事件流、
  `history.jsonl` 探活历史（sparkline + 周报原料）

## 自检

```powershell
python tools\machines_lint.py --remote   # 台账/生成物/六机实配/壁纸 全对账
powershell -File tools\cluster_ping.ps1  # SSH 网格 + 服务健康探活
ssh zhongshu hostname                     # 任意机互访抽查
ssh tts173 hostname                       # 职能别名直呼
```
