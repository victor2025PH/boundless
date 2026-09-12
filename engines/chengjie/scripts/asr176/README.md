# GPU 音频服务（192.168.0.176 / RTX 5090）：ASR + 语音情绪

faster-whisper `large-v3-turbo`(CUDA float16) 的 OpenAI 兼容转录端点(替代本机 CPU whisper 作为主 ASR)
+ emotion2vec_plus_large(CUDA) 的语音情绪端点(替代 117 CPU plus_base 作为主 SER)。

- 端点:`http://192.168.0.176:8765/v1/audio/transcriptions`(契约=OpenAI `audio.transcriptions`,`response_format` 支持 `json`/`text`/`verbose_json`——后者回 `{text,language,duration,segments:[{start,end,text}]}` 供 SRT 字幕消费方,P3 2026-08-18 部署,默认 `json` 响应零变化);`POST /v1/audio/emotion`(multipart file → 原始 `{labels,scores,model,latency_ms}`,标签→系统语义的映射**只在客户端** `src/ai/speech_emotion.py` 单一出口);健康检查 `GET /health`(含 `ser_model`)。
- 消费方:`config/config.local.yaml::voice_recognition`(provider `openai_compatible` 主 + `faster_whisper` CPU 备,经 `FallbackTranscriber` 级联)与 `speech_emotion.remote`(远程优先,失败 120s 冷却回落本地 funasr CPU),两条链都绝不阻塞理解链。
- 实测:ASR 热延迟 ~0.3-0.4s;SER 热延迟 ~44ms 往返(server 15ms)。
- **启动预热(2026-07-11)**:`AITR_WARMUP`(默认 1)在服务启动时后台预载 ASR+SER 两模型,
  消掉重启后首请求 ~15s/~6s 冷启(预热失败仅记日志,懒加载路径兜底重试)。`/health` 增
  `asr_loaded`/`ser_loaded` 可观测装载态。实测预热完成后首请求:SER 0.21s / ASR 0.50s。
- 除 RPA 语音转写:`messenger_rpa.audio_pipeline` 亦指本端点(旧 166 whisper 主机已随网段迁移下线)。

## 176 上的落地物(全部在 C:\aitr_asr\)

| 文件 | 作用 |
| --- | --- |
| `asr_server.py` | FastAPI 服务本体(ASR+SER 各单模型+各自推理锁;ASR 带 VAD 滤静音防幻觉) |
| `start_asr.ps1` | 计划任务入口:环境变量+nvidia wheel DLL 上 PATH+日志到 `logs\` |
| `deploy_asr.ps1` | 幂等部署:uv venv(py3.12)+依赖+防火墙 8765+计划任务注册+启动 |
| `install_emotion.ps1` | 追加安装 torch/torchaudio(cu128, 5090=sm_120)+funasr+modelscope |
| `restart_asr.ps1` / `stop_asr.ps1` | 运维:经计划任务重启 / 停止(演练回落用) |
| `watchdog_asr.ps1` | 自愈看门狗:每 5min 探 `/health`,8s 无响应经计划任务重启(日志 `logs\watchdog.log`;健康时零日志零动作) |
| `prefetch_model.py` | 预下载 ASR 模型到 HF 缓存(SYSTEM 网络上下文不可靠,新模型先以 user 预取) |
| `prefetch_emotion.py` | SER 本地目录 GPU 冒烟(load/infer/标签) |
| `models\emotion2vec_plus_large\` | SER 模型本体(见下「模型获取」) |

开机自启:计划任务 `AITR_ASR_176`(ONSTART, SYSTEM, HIGHEST);
自愈:计划任务 `AITR_ASR_WATCHDOG`(每 5min, SYSTEM)跑 `watchdog_asr.ps1`——
ONSTART 任务只保开机,白天崩了没人管会静默降级 CPU 兜底,看门狗补上这块。
模型缓存:ASR 复用 176 既有机器级 `D:\cache\huggingface`(SYSTEM 有全权;turbo 模型已在)。

**模型获取(176 网络教训)**:176 上 modelscope 直连实测 179kB/s(1.95GB 要 3h)、hf-mirror 直接连接失败
→ **不要依赖 176 的 hub 下载**。在 117 用 `huggingface_hub.snapshot_download`(直连可用,~11MB/s)下到
`D:\tmp\emotion2vec_plus_large`(已留存),`scp -r` 过去(LAN ~2.5min),`AITR_SER_MODEL` 指本地目录。

## 2026-08-29 起服务在 198（`asr198`），2026-09-12 ASR P0 解码纪律

- 服务本体已迁 `192.168.0.198:8765`（计划任务 `AITR_ASR_198`；176 的 `AITR_ASR_176` 已 Disabled）。
  部署：`scp scripts\asr176\asr_server.py asr198:C:/aitr_asr/asr_server.py` →
  `ssh asr198 powershell -NoProfile -ExecutionPolicy Bypass -File C:\aitr_asr\restart_asr_198.ps1`
  （本目录 `restart_asr_198.ps1` 即该脚本）→ `/health` 出 `asr_loaded=true`（~15s）。
- `asr_server.py` 09-12 变更：`condition_on_previous_text=False`；whisper 自带三阈值显式钉住；
  接 OpenAI 标准 `prompt` 字段作 initial_prompt；`verbose_json` 多回
  `language_probability / avg_logprob / no_speech_prob / compression_ratio / vad / rescue`
  （默认 `json` 仍只回 `{text}`）；**短片段二次解码**：<2s 且 VAD 判无人声 → 关 VAD 再解一次，
  只在 `avg_logprob ≥ -0.6 且 language_probability ≥ 0.7 且 compression_ratio ≤ 2.0` 时采信。
- **同机同模型 A/B 结论（勿再踩）**：VAD 放宽（threshold 0.35 / min_speech 100）与钉
  `min_speech_duration_ms=250`（旧版库默认，1.2.1 是 0）都让探针夹具从稳定正确变成乱码/错句；
  任何形式的热词 prompt 在边缘音频上乱码、干净音频只多标点 → 客户端默认不送热词。
  VAD 参数只传旧服务传过的 `min_silence_duration_ms=300`，其余交库默认。

- **换模型先有尺子**（ASR P1/P2）：`python -m scripts.run_eval --asr [--asr-base-url 候选端点]` 用生产同款
  转写链算 CER；金标用 `python tools/asr_gold_curate.py export/import/status` 从归档语音 + 坐席改正积攒
  （≥5 条核对过才裁决）。Qwen3-ASR A/B 步骤见 `docs/指令_ASR_P2_工作台接线契约与QwenASR_AB_runbook_2026-09-12.md`。

## 常用命令(从 117,ssh 别名 gpu176)

```powershell
ssh gpu176 "powershell -NoProfile -ExecutionPolicy Bypass -File C:\aitr_asr\restart_asr.ps1"
Invoke-RestMethod http://192.168.0.176:8765/health
# 换 ASR 模型:改 start_asr.ps1 的 AITR_ASR_MODEL → 先 prefetch 再 restart
# 关 SER:start_asr.ps1 里 AITR_SER_MODEL='off' → restart
```

## 已验证(2026-07-11)

- 5090(sm_120) 上 ctranslate2 CUDA float16 正常出结果(zh/en 双测,语种自检 OK)。
- torch 2.11.0+cu128 在 5090 CUDA 可用;emotion2vec GPU 推理 cold 0.18s / warm 0.01s。
- SER 客户端全链(生产 config → SpeechEmotionRecognizer → 远程):ok, `model=remote:...`,44ms。
- 回落演练:stop 176 服务 → ASR 自动落 CPU whisper、SER 冷却后落本地 funasr → restart 恢复 GPU。
- 重启存活:服务经计划任务以 SYSTEM 跑通(部署即通过任务启动,非手工会话)。
