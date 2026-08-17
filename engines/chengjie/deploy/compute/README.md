# compute —— 智聊算力本地化（两台 5090 拉满，去云端 DeepSeek）

> 2026-08-12 起。上游结论（另一轮攻坚已实证，勿重复验证）：
> - **vLLM 是正解**：Ollama 在 5090 (sm_120) 上内核烂只有 ~10 tok/s；vLLM 实测
>   119 tok/s（官方 Qwen3-30B AWQ）、48.7 tok/s（无审查 32B，WSL enforce-eager）。
> - **无审查模型选定**：`ibrahimkettaneh/Qwen2.5-32B-Instruct-abliterated-pass2-AWQ`
>   （19G，中文强、instruct 无思考、亲密放行实测通过）。
> - WSL2 跑 vLLM 能通但**不是生产级**（空闲杀 VM / UVA pin memory / graph capture 卡 /
>   uvicorn socket handoff 聋服务器四连坑）→ 173 装**原生 Linux**。

## 目标终态（服务落点）

| 主机 | 卡 | 角色 |
|---|---|---|
| 173 yunsheng | 5090 32G | **vLLM 主对话**（无审查 32B AWQ，:8000/v1）——同时服务：主链聊天 / ai.fallback / 语音口语化改写 |
| 176 zhongshu | 5090 32G | 声音+视觉：IndexTTS hub(:9000) / vision qwen3-vl(:11434) / MT hy-mt2(:11434) / 嵌入备点。**ComfyUI 出图停用**（砍出图腾显存） |
| 117 shengbei | 3060 12G | 生产实例宿主 + AvatarHub 7852 情感 TTS 主节点 + GPU ASR 兜底 |
| 140 tingxie | 4070 12G | 嵌入主点 bge-m3 / Whisper STT / MT 兜底 |
| 198 kouxing | 4070 12G | ~~qwen3:8b 口语化改写备点~~（实测未 pull＝死配置，2026-08-15 已从智聊 overlay 摘除；198 补 pull 后可回列） |

## 173 迁移账单（重装前必读——173 不是闲置机）

2026-08-12 实测 173 在线承载三个生产消费面：

| 173 端口 | 服务 | 消费方 | 重装后去向 |
|---|---|---|---|
| :11434 | Ollama `qwen14b-fallback` | zhiliao `ai.fallback` + `avatar_voice.colloquial.llm_endpoints[0]`；**192.168.0.198 也在调**（LiveX/口型线，归属待确认） | **不重建**。zhiliao 侧由 `compute_mode.py` 改指 vLLM `/v1`；198 消费方需知会其负责线改指 vLLM 或 198 本机 |
| :7852 | CosyVoice 情感 TTS | zhiliao `avatar_voice.base_urls` 三号位；**192.168.0.176 高频调用**（幻影 LiveX 线） | 短期：从 zhiliao 列表摘除（117/140 双点仍在）；**动手前必须知会 LiveX 线**；长期可在 Linux 重建（见 RUNBOOK §8） |
| WSL :8000 (loopback) | vLLM 0.27 试验体 | 无（未对 LAN） | 由原生 Linux vLLM 取代，对 LAN 开 :8000 |
| （监控） | `ops.gpu_watermark` 173-5090 走 :11434 | zhiliao ops 卡 | `compute_mode.py cleanup` 摘除该条目（vLLM 用 :8000/metrics，后续再接） |

模型资产：已从 WSL VHDX 撤离到 173 的 **D:\models\**（NTFS 数据盘，878G，重装时勿动），
含 `qwen25-abl-awq`（19G 主力）与 `qwen3awq`（17G，官方 Qwen3-30B AWQ，留作 A/B）。

## 统一契约（2026-08-13 起生效，改动先改这里）

> ⛔ **2026-08-15 启停权移交（现行有效，覆盖本节下方所有历史配方）**：173 两个 vLLM
> unit 的启停/keepwarm 已整体移交**中枢(176) 模式执行器**（`kind=zhiliao_primary`；
> 回执 `D:\chengjie-instances\.ops\REPLY_from_zhongshu_20260815.md`）。我方约束：
> - 计划任务 **`VLLM_Keepalive` 保持 Disabled**（03:10 实测已 Disabled）；其三职（钉
>   WSL VM / portproxy 自刷 / 恢复暖机）由 176 任务 `AvatarHubVllmKeepwarm` 按模式承担。
>   `C:\models\vllm_keepalive.ps1` 留作考古，**不再启用**。
> - `vllm` / `vllm-coder` 两 unit 保持 **disabled**（只 start 不 enable，开机由集群模式
>   恢复）；**不得再 `systemctl enable vllm`**。
> - 顺序契约由中枢执行器执行：停 :8001 前先把智聊切 `cloud`，起 :8001+暖机后切回
>   `local_only`（2026-08-15 02:01:03 首次实弹已验证）。我方另上「:8001 不可达自动热切
>   cloud+告警」保险（上线前先落 `ASK_from_shengbei_*.md`）。
> - 要恢复/切换模式：走 `.ops` 联络单找中枢，**不要**按下方 08-14 的手工配方自行翻转。

> ⚠ **2026-08-14 状态变更**：173 显存已让给编码模式（见下节）。8001/chatx（32B）
> **已停**（权重保留 `D:\models\qwen25-abl-awq`，随时可恢复），zhiliao 主链已热切回
> `ai.primary=cloud`（`POST /api/setup/ai-primary`，操作已于 08-14 03:00 执行并验证）。
> ~~恢复 32B 主链＝`systemctl disable --now vllm-coder && systemctl enable --now vllm`
> + 还原 `C:\models\vllm_keepalive.ps1.bak_32b` + 主链切回 local_only。~~
> （⛔ 2026-08-15 废止：启停权已移交中枢执行器，见本节顶部注记）
>
> ✅ **2026-08-14 14:15 已切回智聊模式**（按上行配方执行）：vllm-coder 停（8010/coder
> 下线，keepalive coder 版备份 `vllm_keepalive.ps1.bak_coder`），vllm(8001/chatx) 起并
> enable，keepalive 已还原 32B 版（portproxy+暖机日志正常）；verify_vllm 59.9 tok/s；
> zhiliao `ai.primary=local_only` 热切生效（copilot 真链冒烟 1.5s，vLLM 侧同刻可见
> completion）。~~回编码模式＝反向执行（还原 `.bak_coder` + `enable --now vllm-coder`）。~~
> （⛔ 2026-08-15 废止，同上）

- **LAN 端点**：`http://192.168.0.173:8001/v1`，served-model-name＝**`chatx`**
  （消灭 8000/8001 双标准：现网 WSL 过渡态与将来原生 Linux 都用 **8001/chatx**；
  本目录 `173-native-linux/*` 的 8000/qwen25-abl-awq 旧参数以本段为准覆盖）。
- **⚡ 2026-08-15 晚主链换代（老板「全集群为智聊」批次）**：`vllm.service` 的底模从
  Qwen2.5-32B-abl（16k, enforce-eager, 61.6 tok/s）换为 **`Huihui-Qwen3.6-27B-abliterated-AWQ`
  （/root/models/qwen36-27b-abl-awq，--max-model-len 65536 上下文 4x、CUDA graph 开、
  --max-num-seqs 32、--reasoning-parser qwen3，实测 81.5 tok/s）**——served 名/端口不变
  （chatx/8001）＝模式执行器/keepwarm/智聊 overlay 全零改动。旧 unit 备份
  `/etc/systemd/system/vllm.service.bak_32b_20260815`，回滚＝cp 回去 + daemon-reload +
  restart。**思考档配套**：Qwen3.6 默认 thinking ON（200 token 全烧在思考里），
  ai_client（oa/fb 两处 think:false 分支）与 voice_colloquial_llm（/v1 分支）已随批
  下发 `chat_template_kwargs: {enable_thinking: false}`（vLLM 消费、Ollama/云忽略）；
  验收题库 6/6：直答 0.33-0.5s、亲密放行、30k 长上下文三事实全召回 3.9s、
  copilot 真链 1.0s。**换代当晚实锤回归**：Qwen3.6 模板**拒绝非打头的 system**
  （`System message must be at the beginning` 400）——本地链的「末位语言钉子」
  正是该形状 → local_only 下带 reply_lang 的客户回复整轮无声（22:02/22:12 实录）。
  修复＝`ai_client._coalesce_system_head`（进 vLLM 前把全部 system 合并进首位，
  内容不丢、顺序保留；门禁 test_ai_client_chat_fallback 三处契约随之更新）。
  教训：**换模型的验证题库必须覆盖「真实组包形状」**（多 system/钉子/注入块），
  只测「单 system+user」会漏掉模板严格性差异。173 另建 **WslPinOnly** 计划任务（ONLOGON，仅
  `wsl -d Ubuntu-24.04 -- sleep infinity` 钉 VM，**不碰任何 unit**——与被禁用的
  VLLM_Keepalive 本质不同；无它则任何 >60s 的冷载都会被 WSL 空闲回收拦腰打断，
  当晚实锤两次）。
- **WSL 过渡态防线**（2026-08-13 实锤后加）：WSL VM 会在最后一个会话退出后
  数分钟被 Windows 回收（`.wslconfig vmIdleTimeout=-1` 实测**不被尊重**），systemd
  随 VM 关机对 vllm 发正常 stop——这就是「服务反复自己停」的全部真相。
  修法＝173 计划任务 **VLLM_Keepalive**（ONLOGON，`C:\models\vllm_keepalive.ps1`）：
  单常驻循环 ＝ 钉 VM 会话 + `systemctl start vllm` 兜底 + portproxy 随 WSL IP 自刷
  + 每分钟 `/v1/models` 健康自检。日志 `C:\models\vllm_keepalive.log`。
  （**2026-08-15 起该任务已 Disabled**：三职改由中枢 176 `AvatarHubVllmKeepwarm`
  按当前模式承担——防线还在，执行者换人，勿再启用本任务。）
- 173 上 EmotionTTS / IndexTTS 计划任务已**删除**（曾在 vLLM 装载中途抢卡触发假死），
  CosyVoice/IndexTTS 启动脚本已改名 `.cmd.off`；恢复走幻影/幻声算力模式流程，
  禁止直接改回。

## 编码模式（coding lane，2026-08-14 起）

- **端点契约**：`http://192.168.0.173:8010/v1`，served-model-name＝**`coder`**
  （`Huihui-Qwen3.6-27B-abliterated-AWQ`，19.6G 权重在 WSL `/root/models/qwen36-27b-abl-awq`）。
  vLLM 参数：`--max-model-len 65536 --gpu-memory-utilization 0.92 --max-num-seqs 64
  --reasoning-parser qwen3`（CUDA graph 开，实测暖态 **~80 tok/s**；`--max-num-seqs 64`
  是 GDN Mamba cache 上限约束，也够多个 coding agent 并发）。
- **思考双档**（实测 2026-08-14）：默认 thinking ON（`reasoning_content` 由 parser 分离，
  编码/规划质量最高）；请求带 `chat_template_kwargs: {"enable_thinking": false}` ＝直答档
  （实测 0.6s 出短答案）——同一实例可兼任低延迟聊天/口语化改写通道，无需第二份显存。
- **持久化**：WSL systemd 单元 `vllm-coder`（`/root/vllm_coder.log`）+
  173 计划任务 `VLLM_Keepalive`（`C:\models\vllm_keepalive.ps1`，已适配 8010：钉 VM 会话 /
  systemctl start 兜底 / portproxy 随 WSL IP 自刷 / 每分钟健康自检 + 恢复即暖机；
  旧 32B 版本备份 `vllm_keepalive.ps1.bak_32b`）。防火墙 8010 已放行。
  （**2026-08-15 起**：两 unit 均 disabled 只 start、`VLLM_Keepalive` 已 Disabled，
  持久化整体由中枢 keepwarm 接管——见「统一契约」顶部移交注记。）
- **验收**：`powershell -File deploy\compute\verify_vllm.ps1 -BaseUrl http://192.168.0.173:8010 -Model coder -MinTokS 50`
- **集群化口径**（如实边界）：LLM 推理**不能跨机拼显存**（1GbE 张量并行会掉到个位数
  tok/s），「全网算力一起上」＝按请求分流/多副本，不是单模型变大。当前唯一能扛 27B 的
  空闲卡就是 173；176(5090) 是语音/视觉生产机（IndexTTS+VLM+MT 峰值 ~25G）动不得，
  140/198/104/117 全是 12G 卡且各有生产角色。可选小模型快车道：198 Ollama（qwen3:8b
  需重新 pull，2026-08-14 实查只剩 MyVisionQwen；其 LAN 访问被 ollama.exe Block 规则
  挡过，已禁用该规则修复）。

## 分阶段

1. **P0 资产保全**（已做，2026-08-12）：模型撤 D 盘；本目录交付物落库。
2. **P1 装机**（人工，见 `173-native-linux/RUNBOOK.md`）：Ubuntu 24.04 Server，只动系统盘。
   ——**过渡态已可服务**（2026-08-13）：WSL vLLM + Keepalive 达到「准生产」，P1 降级为
   提速项（原生 Linux 解锁 CUDA graph，48.7 → 80+ tok/s 预期）而非阻塞项。
3. **P2 bootstrap**：`bootstrap_173.sh` 一条命令拉起 驱动→venv→vLLM→systemd。
4. **P3 验收**：117 上跑 `deploy\compute\verify_vllm.ps1`（连通/出话/tok/s 地板）。
5. **P4 切流**（阶梯升级，2026-08-13）：
   `compute_mode.py prep --apply`（**已执行**：主链不动，兜底+口语化改写先指 vLLM，
   摘 173 死端点；口语化热重载即生效，ai.fallback 随下次 reload/重启装载）→
   `compute_mode.py local --apply` 或 **免重启热切**：`POST /api/setup/ai-primary
   {"mode":"local"}`（写 overlay + reload_ai_runtime 热重建，秒级可回滚）→ 观察 1-2 天 →
   同路径 `local_only`（去云）。
6. **P5 收尾**：`compute_mode.py cleanup --apply`（剩余死引用兜底扫）+
   `--cut-imagegen --cut-fatex`（砍出图/命理，注意 selfie 总闸连带相册）+ 停 176 ComfyUI。

## compute_mode.py（三模式 = ai.primary 的薄壳）

`ai.primary ∈ {cloud, local, local_only}` 是 `src/ai/ai_client.py` 已有基建（P1）：

- `cloud`：云主链 → key 池 → 本地兜底 → canned（现状/回滚档）
- `local`：**本地(vLLM)主链**，失败可回落云端（切流过渡档）
- `local_only`：本地主链，**用户内容绝不发云端**，失败宁可 canned（终态）

本脚本只写 zhiliao overlay（ruamel 保注释），绝不重启进程；`ai.fallback` 即本地主链
的落点，`local/local_only` 模式下会同步指到 vLLM 并重排口语化改写端点。用法：

```powershell
python deploy\compute\compute_mode.py status            # 看当前档位与关键端点 + vLLM 探活
python deploy\compute\compute_mode.py prep --apply      # 第一级：主链不动，兜底+改写换血
python deploy\compute\compute_mode.py local             # dry-run（只打印 diff）
python deploy\compute\compute_mode.py local --apply     # 落盘；免重启热切见上方 P4（ai-primary 路由）
python deploy\compute\compute_mode.py cleanup --apply --cut-imagegen --cut-fatex
```

覆盖面如实声明（与 ai_client 注释同口径）：`ai.primary` 只管**主对话链**。嵌入/视觉/
翻译/TTS 各有自己的端点配置（本就全在 LAN），不由它代管。

## 回滚

任何阶段：`compute_mode.py cloud --apply` + 标准重启 = 回 DeepSeek 云主链（key 原位
未动过）；**免重启热回滚**＝`POST /api/setup/ai-primary {"primary":"cloud"}`（Bearer
admin token，写 overlay + reload_ai_runtime 秒级生效）。vLLM 侧问题查
`journalctl -u vllm -n 100`；08-15 起钉桩/暖机归中枢 `AvatarHubVllmKeepwarm`（176），
`C:\models\vllm_keepalive.log` 只剩历史价值。

## 切流实录（2026-08-13 04:30，主链已切 local）

- `POST /api/setup/ai-primary {"primary":"local"}` → `effective=local, ai_ready=true`，
  **零重启零停机**；copilot 真链冒烟 1.9s 出话，vLLM 侧日志同刻可见 completion。
- 切流前把 `--max-model-len` 8192→**16384**（主链 prompt 人设+记忆+KB 可能超 8k，
  云端 64k 上下文一直掩盖此事；KV 预算 39,808 tokens，16k 下并发 2.4x）。
- 暖态吞吐 **61 tok/s**（verify_vllm.ps1 自带 untimed warmup——vLLM 0.27 进程首个
  长生成要 JIT 编内核，实测冷 7.3 vs 暖 56-61；keepalive 已带「恢复即暖机」）。
- 观察 1-2 天达标后：`POST /api/setup/ai-primary {"primary":"local_only"}` 去云。

## 176 语音侧联动（2026-08-13 凌晨，Stage 3 半程）

173 清场把 hub（176:9000）钉在 173 的三个远端引擎一并打死（engines 注册表：
index_tts2→173:7865 / moss_ttsd→173:7866 / cosyvoice→173:7852）。已做：

- hub 路由热改（`PATCH /api/config`，hub_config.json 层每次启动都赢）：
  `emotion_tts → http://192.168.0.140:7852`（活的 CosyVoice，models_loaded）；
  `index_tts → http://127.0.0.1:7865`（解开「已迁远端 SVC_* 不本机启动」钉，
  为 176 本地重建铺路）。
- `voice_route.quality_engine: index_tts → cosyvoice`（**临时**，盲听 0.878 次优；
  实测 hub 全链 58-80s/轮——4070 扛质量轨太慢，只够 LiveX 离线出片，不够聊天实时）。
- **zhiliao 语音主链无损**（实测澄清）：其 TTS=117 本机 7852（avatar-status 全绿，
  夜间声纹探针 0.87-0.96），173:7852 只是已摘除的第三备点；hub_fish 仅 8 白名单
  人设的优选路径，strict 语义下 hub 失败回落不发错声。
- **已恢复（2026-08-13 14:15，Stage 3 完成）**：IndexTTS-2 **整目录移植** 173 → 176
  （tar 17.7GB：项目+`.venv`+checkpoints+secrets → 176 **同绝对路径** `C:\models\index-tts`，
  uv 基座 CPython 3.11.13 一并放同路径 `C:\Users\admin\AppData\Roaming\uv\python\...`——
  同路径移植＝零路径手术，venv 冒烟 torch 2.8.0+cu128 CUDA True 一次过）。
  服务走 `.venv\Scripts\python.exe` 直跑（**刻意绕开 `uv run`**——它可能按锁重建环境）；
  计划任务 **IndexTTS**（176 ONLOGON，`_serve_task.cmd` 带双开守卫）。
  `voice_route.quality_engine` 已切回 `index_tts`（cosy 止血档退役，但 hub `emotion_tts`
  路由保留 140:7852 作活备）。实测：hub 全链合成 **冷 8s / 暖 4.8s**（止血档 58-80s；
  173 时代基准 8-10s/轮），176 VRAM 稳态 ~24.7G / 30G cap。
  幻声线注意：hub 引擎注册表元数据仍显示 index_tts2 base=173:7865（陈旧显示，
  生效路由在 hub_config.json 层=127.0.0.1:7865）；跨机顶层清理属你们的 SSOT。

## 观察期读数（2026-08-13 14:20，切流 +10h）

- vLLM 侧：**65 次 completion**（8:43/11:45/12:09 均有真实客户流量），prefix cache
  命中 ~36%（人设块复用生效），keepalive 日志零异常、服务零重启。
- inbox 侧：切流后 20 进 / 38 出，与 vLLM 请求量吻合。
- ⚠ 观测债：`/api/setup/cloud-credentials.usage` 的 `local_primary` 只记到 1 次调用
  ——llm_cost 对「local 主链」的 tier 归账口径与实际调用量对不上（事实以 vLLM 侧
  日志为准），修归账属后续小项，不影响切流判定。
- ~~`local_only` 去云判据：再观察至明天~~ → **已于 2026-08-13 14:59 切 `local_only`
  开始内测**（运营决策提前）：`effective=local_only`，三风格真链冒烟全过
  （中文闲聊 1.4s / 英文应答语言正确 2.3s / 「你是不是AI」按人设底线答 0.6s），
  runtime `degraded=false`。主对话链用户内容自此不出内网；vLLM 失败时按
  local_only 语义回 canned（**不**回落云端），可用性靠 173 keepalive+systemd 自愈。
  回滚：`POST /api/setup/ai-primary {"primary":"local"}`（恢复云端回落）或 `cloud`。
  ⚠ 如实边界：`ai.primary` 只管主对话链。翻译引擎序 `[ollama_mt, ai]` 的末位
  云兜底仍在（仅当 176+140 两台 LAN MT 都挂才会碰云）；记忆抽取/视觉等按各自
  配置早已全 LAN。要绝对零云可再摘翻译 `ai` 兜底，代价=LAN MT 全挂时翻译不可用。

## 事故记：switching 卡死死锁（2026-08-15 晚，中枢执行器侧；诊断口诀留档）

**现象**：hub `/health` 的 `cluster_mode.switching` 卡 `true`（progress 挂着失败的
y 步）→ 176 `vllm_keepwarm` 按设计静默（tick 开头判 switching 即 return）→ 173 WSL VM
被 idle 回收无人再钉 → 双 vLLM 全停、5090 空转 ~1.5h；gate 又因「已有一次切换在进行
中」拒绝新切换＝**死锁**。当晚 hub 进程自身还静默死过一次（19:33–19:53，被
AvatarHubWatchdogGuard 拉回后自动续跑切换收敛）。

**根因三层**：① y 步 300s 就绪窗 < VM 冷启+27B 冷载最坏路径（预热后实测仅 ~50s）；
② switch run 中断后 switching 无 finally 清理（孤儿态无人接管）；③ keepwarm 的
`sleep infinity` 钉桩随 SSH 会话回收从未存活（PIN_NEW 连续出现即此症；实际保活靠
1min tick 本身）。四条修复建议已发单 `D:\chengjie-instances\.ops\ASK_from_shengbei_20260815.md`
（同单含智聊/编程两模式需求清单正式版）。

**同晚第二起（117 本机，与 176 无关）**：zhiliao python 实例 20:42 起半挂死（心跳/日志
全停、web 监听丢失、进程残存），21:20 本机桌面壳（telegram-ai-desktop）探测到 18799
空闲按设计自起 sidecar `backend.exe` 占位——**watchdog 探活只看 /login 200，被错误进程
的登录页骗过**，40 分钟无人拉起真实例。处置＝杀僵尸+杀占位 sidecar → `start_zhiliao.ps1
-DataDir D:\chengjie-instances\zhiliao\data`（⚠ 本机部署**必须显式传 -DataDir**：脚本缺省
指 deploy\instances\zhiliao\data，不存在则防呆拒启）。挂死根因未定位（进程已杀失去现场），
再犯时先抓 `py-spy dump`（本机已装 0.4.2）再杀。改进项：watchdog 探活应校验 18799 归属
进程 cmdline 含 main.py（防 sidecar 顶包假绿）。

**我方处置口诀**（再遇到照抄，全程勿 enable 任何 unit / 勿碰 VLLM_Keepalive）：
1. 判死锁：`GET 176:9000/api/cluster/mode?gate=1` 看 switching 卡 true + progress 停在
   失败步；`ssh zhongshu 'Get-Content D:\projects\*\logs\vllm_keepwarm.log -Tail 5'`
   看 keepwarm 是否静默；`ssh yunsheng "wsl -l -v"` 看 VM 是否 Stopped。
2. 预热 173（替执行器把冷路径变热路径）：后台挂
   `ssh yunsheng "wsl -d Ubuntu-24.04 -- sleep 1800"` 钉住 VM →
   `ssh yunsheng "wsl -d Ubuntu-24.04 -u root -- systemctl start vllm-coder.service"`
   （chatx 模式则 vllm.service；**只 start**）→ 轮询 `:8010(/:8001)/v1/models` 至 200
   （portproxy 仅在 `wsl hostname -I` 变化时才需对齐）。
3. 让执行器收口：hub 活着 → `POST /api/cluster/repair`（force 越闸门重放当前模式，
   会顺带清 switching）；hub 死了 → 等 `AvatarHubWatchdogGuard`（分钟级）拉回后它会
   自动续跑；完成判据＝mode 落定 + switching=false + keepwarm 恢复出 tick 日志。
4. 收尾：撤钉桩（kill 本地 ssh 即可），互验 8010/8001 与 176 `/api/ps` 钉模，事件发
   `.ops` 联络单知会中枢。
