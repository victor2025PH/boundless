@echo off
rem ==========================================
rem  deploy.env overrides (loaded LAST by env_config.bat, wins over all).
rem  Created for streaming-TTS A/B measurement on the GPU box.
rem  Delete this file (or set CONV_TTS_STREAMING=0) to revert to whole-sentence.
rem  ASCII-ONLY: non-ASCII in a rem line desyncs cmd's batch parser under chcp 65001,
rem  which silently skips the `set` lines below AND aborts `call env_config.bat`
rem  mid-chain (exit 255) so the hub never launches. Keep this file ASCII.
rem ==========================================
set CONV_TTS_STREAMING=1
rem  Single-card admission policy (A3 soak, 2026-06): on one 5090 with admission OFF,
rem  N>=2 independent parallel chats all degrade together (first-frame p95 1.6s -> up to
rem  38s) because TTS/lipsync share one GPU and serialize. With auto admission
rem  (K = pool size, single card = 1) only 1 route runs; the rest enter a fair queue
rem  with live ETA, so first-frame stays stable and predictable. That is the correct
rem  single-card shape: 1 realtime avatar + audience questions answered serially.
rem  True concurrency needs a 2nd card (multi-replica SVC_LIPSYNC).
set CONV_MAX_CONCURRENT=auto
set CONV_MAX_QUEUE=20
rem  HD boot self-prewarm: this box runs realtime HD, so load GFPGAN + autotune once at
rem  startup so the first HD sentence is already full-speed.
set LIPSYNC_HD_PREWARM=1
rem  Multi-replica / multi-card lipsync: when a 2nd video card or box is added, uncomment
rem  the line below with two URLs for true parallelism (load-balance + failover verified).
rem  Note: on multi-replica, disable idle-unload on the 2nd box (set LIPSYNC_IDLE_UNLOAD=0),
rem  otherwise idle unload gets misjudged by health probes and the replica is ejected.
rem set SVC_LIPSYNC=http://127.0.0.1:8090,http://192.168.0.XXX:8090
rem  2026-07-05: lit up the .198 hot standby (RTX 4070, D:\faceX start_lipsync_standby.bat,
rem  idle-unload off by default there) as a 2nd musetalk replica -> conversation concurrency
rem  K goes 1 -> 2 (auto admission follows healthy pool size). If .198 is off/unreachable the
rem  hub probe ejects it and everything degrades back to single-replica (zero regression).
rem 2026-07-09 wechat-first replan: digital-human lipsync served by .198 replica only; local 8090 parked (-8.7G VRAM on .176).
rem 2026-07-15 incident fix: .198 unreachable for days (connect timeout) while local 8090 is
rem   actually up (models_loaded, cuda) -> every interp session wasted 3x180s on face-precompute
rem   against a dead host. Local-first + .198 as replica: hub pool fails over automatically,
rem   interp takes the first URL (local). When .198 comes back it rejoins as overflow replica.
set SVC_LIPSYNC=http://127.0.0.1:8090,http://192.168.0.198:8090
rem  Heterogeneous pool (5090 primary + 4070 replica): break inflight ties by list order
rem  instead of round-robin, so a single session always lands on the fast 5090 and the 4070
rem  only takes overflow when the 5090 is busy. rr (default) = homogeneous-cards behavior.
set SVC_POOL_TIEBREAK=first
set CONV_TTS_STREAM_FIRST_MS=300
set CONV_TTS_STREAM_CHUNK_MS=550
set CONV_TTS_STREAM_MAX_MS=1500
rem  Lipsync(video)-mode adaptive coalescing uses code defaults (STD 600/1000/1800ms,
rem  HD 1500/4000/5000ms).
rem  A/B (2026-06): shrinking STD from 600/1000 to 300/600 cut content-sentence
rem  text->first-lip by only ~60ms (noise) while adding ~67% more lipsync calls / vcam
rem  pushes, because content first-frame is gated by the single-card render queue (behind
rem  the opener + prior sentences), not by coalescing. So keep the bigger defaults (fewer
rem  calls, easier on one card). To override manually, uncomment below:
rem set CONV_TTS_STREAM_LIP_FIRST_MS=600
rem set CONV_TTS_STREAM_LIP_CHUNK_MS=1000
rem set CONV_TTS_STREAM_LIP_MAX_MS=1800

rem ==========================================
rem  Semantic hybrid RAG (Phase 9-6..9-10). Enables BM25 + bge-m3 vector fusion (RRF)
rem  + MMR rerank + long-term memory semantic recall/dedup/conflict. Reuses local Ollama
rem  (ollama pull bge-m3). If the embeddings endpoint is unreachable at boot the hub
rem  auto-falls back to pure BM25 (zero risk). Verified online 2026-06-28 (8/8 acceptance).
rem  Comment out / set empty to revert to BM25-only.
set CONV_EMBED_MODEL=bge-m3
set CONV_EMBED_BASE_URL=http://127.0.0.1:11434

rem ==========================================
rem  Realtime-HD default engine (2026-06-29). Make ditto (realtime full-face 512, warm
rem  RTF~1.0 measured on this 5090) the default lipsync engine for realtime / streaming /
rem  conversation. HEALTH-GATED: if the ditto service (port 8096) is not online, realtime
rem  auto-falls back to musetalk (256) with zero regression -- so enabling this is safe
rem  even when ditto is not running. Start ditto with start_ditto.bat; /api/gpu/release
rem  stops it too (frees ~5GB). Explicit per-request / per-profile lipsync_engine still
rem  wins. Out-of-band (file) synthesis default is separate (AVATARHUB_LIPSYNC_DEFAULT).
rem  Set empty (or delete this line) to revert realtime to musetalk.
rem ==========================================
rem  Interp MT model right-sizing (2026-07-05 A/B, see logs/optimize_20260705/):
rem  qwen2.5:32b q4 (20.9GB) does not fit next to fish+nemo+ditto+musetalk on the
rem  32.6GB card, so ollama spilled 32% of weights to CPU -> 6.8 tok/s, median
rem  3632ms/sentence (max 5549ms). qwen3:32b is the same size and spills the same
rem  (median 3695ms) -> switching 32b generations does NOT fix latency.
rem  qwen2.5:14b fits 100% in VRAM -> 140 tok/s, median 236ms/sentence (15x faster),
rem  sampled translation quality on live-commerce/meeting sentences is on par, and
rem  Z1Q/Z2Q glossary placeholders survive intact. Whole box drops to ~26.7/32.6GB,
rem  so ditto+musetalk+nemo all stay resident (no capability trade-off).
rem  Brand terms remain guarded by glossary locking + survival probe; baselines are
rem  tracked per-backend via INTERP_MT_TAG. Revert = delete the two set lines below.
rem  Next candidate once Nemotron moves off this box (P1): qwen3:30b-a3b (MoE).
rem  MT-specialist candidate A/B (2026-07-07, logs/optimize_20260707/mt_ab_results.jsonl):
rem  Tencent Hy-MT2 7B GGUF (kaelri builds) is BROKEN under ollama (needs llama.cpp STQ
rem  kernel PR: empty outputs on placeholder/long sentences + hy_begin/end token leakage).
rem  Hy-MT2 1.8B works and is 2x faster (131ms med, 410 tok/s, 1.7GB) BUT mistranslated
rem  "li jian 50 yuan" as "50% off" (price-changing hallucination = disqualifying for
rem  live commerce) and dropped 1/4 glossary placeholders.
rem  Round 2 same evening: demonbyron/HY-MT1.5-7B:Q4_K_M (different GGUF conversion) DOES
rem  work: 193ms med, 253 tok/s, 5.1GB VRAM, numbers faithful ("li jian 50" -> "discount
rem  of 50 yuan"), but placeholder survival 13/14 vs qwen2.5:14b 14/14 (dedicated probe
rem  _p1_mt_placeholder_probe.py). Switching MT to it would ALSO keep qwen2.5:14b resident
rem  for GER arbitration (translation-specialist cannot proofread) -> +5.1GB on a full
rem  card for a 74ms gain + a placeholder regression. Verdict: qwen2.5:14b stays MT+GER
rem  primary; demonbyron/HY-MT1.5-7B:Q4_K_M is the validated emergency fallback when VRAM
rem  is tight (swap via INTERP_LLM_MODEL, hy-mt prompt template auto-applies).
rem  STQ tracking (2026-07-07 P4): llama.cpp upstream MERGED the Hunyuan STQ1_0 kernels
rem  (CPU PR#22836, CUDA MMVQ PR#23505 ~May 2026, speed parity with Q4_K_M).
rem  P5 LAB RETEST (2026-07-08 01:5x, logs/optimize_20260707/mt2_lab_results.jsonl):
rem  same Hy-MT2-7B Q4_K_M GGUF blob on llama.cpp b9902 (standalone CPU lab, port 8180,
rem  tools/_p5_mt2_lab_probe.py, production untouched) -> FULLY FIXED: placeholders 14/14,
rem  numbers faithful ("li jian 50"->"discount of fifty", 3200 intact), 347-char long
rem  sentence complete, ZERO hy_begin/end token leakage. Root cause was ollama 0.24.0's
rem  old bundled engine.
rem  MAINTENANCE WINDOW DONE (2026-07-08 02:1x-02:4x): ollama upgraded 0.24.0 -> 0.31.1
rem  (models intact, qwen2.5:14b regression-tested OK). GPU A/B verdict:
rem   - kaelri/hy-mt2:7b-q4_K_M (their template): STILL broken on 0.31.1 (8/14, token
rem     leak) -> their Modelfile template was a second, independent bug. Tag removed.
rem   - hy-mt2-7b-official (SAME blob + official jinja template rewritten for ollama,
rem     lab/llamacpp/Modelfile.hy-mt2-7b): 13/14 placeholders, numbers faithful, no
rem     leakage, fast on GPU. Replaces HY-MT1.5-7B as the preferred low-VRAM fallback.
rem   - qwen2.5:14b: 14/14 -> STAYS primary (placeholder bar not met by MT2; VRAM also
rem     cannot host both MT2 + qwen-for-GER). Revisit after nemotron moves off this box.
rem P4 2026-07-10: 14b->7b for GER/mood (pinyin gate bounds quality risk; halves latency, frees ~4.5G VRAM
rem for local lipsync+emotionTTS co-residency). Revert: set back to qwen2.5:14b.
set INTERP_LLM_MODEL=qwen2.5:7b
set INTERP_MT_TAG=llm:qwen2.5:14b
rem  Context alignment (2026-07-05 root-cause fix): ollama reloads a model whenever the
rem  requested num_ctx differs from the loaded runner, and calls WITHOUT options (hub
rem  warmup/keepwarm /api/generate) load at the OS default -- which on ollama 0.24 is the
rem  model's full declared context (32768!). That silently ballooned qwen2.5:14b 9.3G->17G
rem  (and historically qwen2.5:32b 20G->31G, the CPU-spill root cause) and forced a full
rem  reload every time interp(2048) and conversation(default) alternated. Fix is two-sided:
rem  OLLAMA_CONTEXT_LENGTH=4096 is set machine-wide (setx, picked up by ollama at login) so
rem  optionless calls load at 4096, and interp asks for the same 4096 below -> one stable
rem  runner serves both paths, zero reloads. 14b KV at 4096 is ~0.4G (nothing).
set INTERP_LLM_NUM_CTX=4096

set AVATARHUB_LIPSYNC_RT_DEFAULT=ditto
rem  Keep ditto always-on + self-healing (in-Hub supervisor revives it; GPU engage(all) starts
rem  it) so the realtime-HD default is actually "always there", not just when manually launched.
rem  GPU release still stops it (frees ~5GB for the other machine) and the released flag keeps it
rem  stopped until engage. Set 0 (or delete) to make ditto on-demand via start_ditto.bat instead.
rem 2026-07-09 wechat-first replan: ditto HD lipsync parked (was 4.7G VRAM idle on .176); set back to 1 to re-enable.
set HUB_SUP_DITTO=0
rem  2026-07-07 incident hardening: vcam(7870) died mid-stream and nothing revived it --
rem  every lipsync segment push got connection-refused while sentences kept rendering,
rem  jamming the 5090 (gpu_wait 29s->110s) and hanging role activation at "preparing 92%".
rem  This deploy runs the digital-human (vcam) mode, so supervise vcam like ditto: the
rem  in-Hub supervisor revives it with backoff/breaker. The real-camera faceswap mode's
rem  OBS device fight (why env_config defaults it to 0) is already handled at runtime:
rem  _free_obs_from_vcam() parks vcam in _PARK_SUSPEND, which the self-heal loop skips.
set HUB_SUP_VCAM=1
