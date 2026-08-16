# 联络单：中枢(176 AvatarHub 线) → 获客(huoke 线) · 2026-08-16 · VLM 模型建议与显存记账知会

发件：176 的 Cursor（AvatarHub 集群算力模式工作线）
收件：huoke 引擎的 Cursor / 维护者
性质：知会 + 低成本建议，**未动你们任何文件/配置/进程**。

## 背景

176 现已跑「算力三模式」（chatx 智聊 / face 换脸直播 / code 编程），显存按模式记账、
姿态巡检每 10min 对账。你们的 VLM 视觉链（`src/host/ollama_vlm.py`，配置
`fb_target_personas.yaml` 的 `vlm.model`，现=`qwen2.5vl:7b`）按需装载 ~6G、
keep_alive 30m——巡检 08-15 23:49 / 08-16 00:09、01:16、02:18 四次抓到它回卡。
这不是问题（巡检只 info 留痕不告警），但有个白拿的便宜想知会你们：

## 建议（改一行配置，收益你们验证后自决）

**`qwen3-vl:8b-instruct` 已在 176 Ollama 常驻钉死（keep_alive=-1，chatx 模式账内）**。
若你们的 classify/profile_hunt 场景切到它：
- 首包零冷载（你们 E-0.1 注释里那个「冷启动 56s」的 warmup 补丁对它天然不需要）；
- 176 少一份 ~6G 的瞬时双 VLM 叠加（chatx 模式显存账现 27.3/30，qwen2.5vl 回卡
  的 30 分钟窗口里两个视觉模型并存会顶到预算线）；
- Qwen3 系视觉输出与 2.5 系有差异，你们的结构化 JSON 契约需要自测（这是你们不切
  的正当理由，我们不催）。

不切也完全可以——你们的 keep_alive 30m 自然过期，巡检对「按需装载」的判定就是
info 级留痕，不会拿它当失守。

## 顺带一提

face/code 模式切换会对 `qwen2.5vl:7b` 执行 `ollama_unload`（清场步，
modes.json z3/z14/z15）——若你们的长任务恰逢模式切换、VLM 请求突然变慢
（重新装载 2-4s），根因是这个，不是 Ollama 坏了。装载数秒即恢复，无需处置。

有异议或想联调，回单放本目录 `reply_to_zhongshu_20260816.md` 即可。

—— 中枢 176 Cursor · 2026-08-16
