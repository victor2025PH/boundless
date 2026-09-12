# ASR P2 交接：工作台接线契约（给 inbox 线）+ Qwen3-ASR A/B runbook

> 2026-09-12 深夜。P0（`1ca3425b`）/ P1（`037ef2c5`）/ P2（本批）三批 ASR 改动的后端已齐；
> `unified_inbox.html` / `inbox_workspace.py` 当晚被两条线持续编辑（Q-31 / Q-35），本线**没有**
> 碰模板——下面是前端接线所需的全部后端契约，照抄即可，零后端改动。

## 一、thread 响应：语音行多了 `asr` 字段

`GET /api/unified-inbox/thread` 每条**入站语音/音频**行（`media_type ∈ {voice, audio}`）在有元数据时带：

```json
"asr": {
  "confidence": "high|medium|low|",     // 三档；空=无证据（P2 之前的老行）
  "suspect": "low_confidence|lang_conflict|no_speech|short_unsure|",  // 空=不可疑
  "language": "zh", "language_probability": 0.98, "avg_logprob": -0.22,
  "no_speech_prob": 0.0, "duration": 11.0, "provider": "OpenAITranscriber", "level": 0,
  "cache_hit": true, "lang_retry": true, "low_confidence": true, "lang_suspect": true,   // 缺省不带
  "corrected": false, "corrected_by": "op1",     // 坐席改正过 → true + 改正人
  "machine_text": "机器原转写（改正后仍保留，供对照）",
  "ts": 1789230000.0
}
```

建议渲染（媒体行「语音转写」样式旁）：
- `confidence=low` 或 `suspect` 非空 → 黄色小徽标「转写可疑 · 可能听错」，悬浮显示原因码人话：
  `low_confidence`=置信度低 / `lang_conflict`=语种与会话对不上 / `no_speech`=疑似无人声 / `short_unsure`=片段太短；
- `corrected=true` → 灰标「已人工改正」，悬浮显示 `machine_text`（机器原话）；
- 行内「✎ 改正」入口 → 弹输入框预填当前正文 → 调下面的 POST → 成功后 `loadThread()`。

`_L1R_KEYS` 需加 `asr_suspect: 'inbox.l1r.asr_suspect'`（i18n zh「语音转写可疑，先过一眼」/ en
"Voice transcript uncertain — review first"），否则草稿卡显示成通用「其他原因: asr_suspect」。

## 二、改正写口

`POST /api/unified-inbox/asr-correction`
```json
{"platform": "whatsapp", "account_id": "639270135480", "chat_key": "639273815533",
 "message_id": "<thread 行的 message_id（store id，不是边车 wamid）>",
 "corrected_text": "人听到的话"}
```
响应 `{ok, conversation_id, message_id, machine_text, corrected_text, audio_sha1, ledger, text_updated,
meta_updated, cache_updated}`。服务端一次做完四件事：写改正台账（评测金标）/ 回写消息正文 /
元数据清可疑记改正人 / 同一音频的转写缓存改成人改的话。错误：400 缺字段·非语音·超 2000 字，
404 消息不在该会话（越权），503 inbox 未就绪；文案全部 `tr()` 双语。

`GET /api/admin/asr-corrections?limit=100` → `{ok, total, items[…台账逐条, 不含本地音频路径],
hotword_candidates[{word,count}], ledger_path}`——ops 可加一张「转写改正 / 热词候选」卡。

## 三、金标积累与 Qwen3-ASR A/B（runbook）

1. `python tools/asr_gold_curate.py export --days 7 --out tmp_diag/asr_candidates.jsonl`
   → 人耳听 `audio`，把听到的话填进 `ref`（机器对的直接抄；已改正的行已预填）
   → `python tools/asr_gold_curate.py import tmp_diag/asr_candidates.jsonl`
   → `python tools/asr_gold_curate.py status`（≥5 条核对过且音频在＝可裁决）。
   **先核对探针夹具**：`assets/probe/asr_probe.txt` 写「你好你好，今天天气不错」，198 whisper-turbo
   稳定转「嗯嗯今天天氣不錯啊」，两者必有一错（CER 0.5 就是这么来的）；核对后改清单 ref 或改 sidecar。
2. 基线：`python -m scripts.run_eval --asr`（生产链，198 large-v3-turbo，缓存关）。
3. 候选：198 是 Windows + RTX 4070 12G（已驻 turbo 3.2G + emotion2vec ~1G）。vLLM 不支持 Windows，
   Qwen3-ASR-1.7B 走 `qwen-asr` transformers 后端（bf16 ≈ 4G），模型在 117 用
   `huggingface_hub.snapshot_download` 下好再 scp（198 hub 直连不可靠，见 asr176/README）。
   服务端要暴露 OpenAI 兼容 `POST /v1/audio/transcriptions`（复用 `asr_server.py` 的多部件解析 +
   `verbose_json` 置信度字段形状），`prompt` 字段映射到 Qwen3-ASR 的 `context=` 偏置——这才是
   热词问题的正解（whisper prompt 已被 09-12 A/B 数据否决）。
4. A/B：`python -m scripts.run_eval --asr --asr-base-url http://192.168.0.198:<port>/v1 --json`
   与基线比 `mean_cer` / `by_lang` / `by_bucket`；粤语、短片段、含产品名三桶单独看。
5. 赢了再切：overlay `voice_recognition.base_url` 指新端点（工厂 `qwen3_asr` 别名已在），
   turbo 降为 fallback 第一级、140 第二级；灰度期看 ops「语音转录降级」卡回落率与低置信率。
