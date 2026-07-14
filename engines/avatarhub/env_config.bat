@echo off
rem ==========================================
rem  Shared environment config - all launchers `call` this file.
rem  P0 portability: project root + conda paths are auto-detected, so moving
rem  to another machine / user / drive needs NO edits here.
rem  ASCII-only on purpose (encoding-proof: safe to call before any chcp).
rem  To override manually: `set BASE_DIR=...` / `set CONDA_ROOT=...` before calling.
rem ==========================================

rem -- Project root = this script's own folder (%~dp0 has a trailing backslash) --
if not defined BASE_DIR set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"

rem -- Detect conda root: prefer existing CONDA_ROOT, then common locations --
if not defined CONDA_ROOT if exist "%USERPROFILE%\Miniconda3\envs" set "CONDA_ROOT=%USERPROFILE%\Miniconda3"
if not defined CONDA_ROOT if exist "%USERPROFILE%\Anaconda3\envs" set "CONDA_ROOT=%USERPROFILE%\Anaconda3"
if not defined CONDA_ROOT if exist "C:\Users\user\Miniconda3\envs" set "CONDA_ROOT=C:\Users\user\Miniconda3"
if not defined CONDA_ROOT if exist "C:\ProgramData\Miniconda3\envs" set "CONDA_ROOT=C:\ProgramData\Miniconda3"
if not defined CONDA_ROOT set "CONDA_ROOT=%USERPROFILE%\Miniconda3"

rem -- Per-env python.exe (built from CONDA_ROOT) --
set "FACEFUSION_PY=%CONDA_ROOT%\envs\facefusion\python.exe"
set "RVC_PY=%CONDA_ROOT%\envs\rvc\python.exe"
set "FISHSPEECH_PY=%CONDA_ROOT%\envs\fishspeech\python.exe"
set "COSYTTS_PY=%CONDA_ROOT%\envs\cosytts\python.exe"
set "MUSETHEPEAK_PY=%CONDA_ROOT%\envs\musethepeak\python.exe"
set "YMSVC_PY=%CONDA_ROOT%\envs\ymsvc\python.exe"
set "LATENTSYNC_PY=%CONDA_ROOT%\envs\latentsync\python.exe"
rem -- VoxCPM2 commercial-licensed clone TTS (own env, py3.11) + Nemotron3.5 streaming STT (own env, py3.11) --
set "VOXCPM_PY=%CONDA_ROOT%\envs\voxcpm\python.exe"
set "NEMOASR_PY=%CONDA_ROOT%\envs\nemoasr\python.exe"
rem -- Qwen3-TTS dual-track low-latency streaming clone TTS (own env, py3.10+) --
set "QWEN3TTS_PY=%CONDA_ROOT%\envs\qwen3tts\python.exe"
rem -- EchoMimic full-face HD video avatar (separate ASCII repo + its own env) --
set "ECHOMIMIC_PY=%CONDA_ROOT%\envs\echomimic\python.exe"
if not defined ECHOMIMIC_DIR set "ECHOMIMIC_DIR=C:\echomimic"
rem -- Ditto motion-space-diffusion REALTIME full-face talking head (own ASCII repo + env) --
set "DITTO_PY=%CONDA_ROOT%\envs\ditto\python.exe"
if not defined DITTO_DIR set "DITTO_DIR=C:\ditto"
rem -- FitDiT virtual try-on (2026-07-08): SD3-DiT dual-tower, all-onnx preprocess (no detectron2).
rem    Weights 8.1G @ C:\models\FitDiT (+CLIP encoders in HF cache ~11G). Host env "fitdit"
rem    (cloned from musethepeak: diffusers 0.38 / torch 2.11+cu128 / ort-gpu) keeps the live
rem    lipsync env untouched. FITDIT_OFFLOAD=1 (default) peaks <6G VRAM so it can run mid-show;
rem    set 0 on an idle box for full-residency speed. License: CC BY-NC-SA (non-commercial).
set "FITDIT_PY=%CONDA_ROOT%\envs\fitdit\python.exe"
if not defined FITDIT_DIR set "FITDIT_DIR=C:\models\FitDiT"
if not defined FITDIT_CODE_DIR set "FITDIT_CODE_DIR=C:\FitDiT"
rem set FITDIT_OFFLOAD=0
rem set FITDIT_STEPS=20
rem set FITDIT_RESOLUTION=1152x1536

rem  Ditto LIVE-FIRST scheduler (anti GPU-lock head-of-line block; same class as the MuseTalk fix).
rem  Ditto serializes ALL GPU work on one asyncio.Lock (FIFO, no priority), so a mid-show /ditto/register
rem  (character activation) can queue ahead of / hold the lock against an arriving live stream sentence
rem  -> first-frame stall. DITTO_BG_DEFER=1 (default, baked into ditto_server.py) makes register
rem  async-yield while a live sentence is active (last DITTO_LIVE_GUARD_SEC=2.0s), capped by
rem  DITTO_BG_DEFER_MAX_SEC=20s so register never starves; generate_stream now also returns lock_wait_ms.
rem  Validated 2026-07-02 (isolated 8097 A/B): register deferred 8.77s past a live show, live lock_wait 0ms
rem  (vs FIFO 47ms). NOTE: ditto register is LIGHT (~tens of ms for a warmed face), so the practical gain is
rem  modest + observability -- the guarantee scales with whatever the bg task costs. DITTO_BG_DEFER=0 reverts
rem  to plain FIFO (zero-regression). Takes effect on next ditto restart (running 8096 keeps old behavior).
rem set DITTO_BG_DEFER=0
rem set DITTO_LIVE_GUARD_SEC=2.0
rem set DITTO_BG_DEFER_MAX_SEC=20
rem  Ditto FIRST-FRAME tuning (A-1): first streamed segment size (frames) the Hub sends to generate_stream.
rem  8097 A/B 2026-07-02: 6->4 cuts seg0-delivered ~75ms (long sentences -10~18%), no under-run (renderer
rem  ~2x realtime) and no A/V drift (seg0 still carries the whole-sentence audio). Default 4; try 3 to be more
rem  aggressive on long sentences. generate_stream now returns first_frame_ms/seg0_ms (always-on, no debug flag)
rem  and the Hub records seg0 into _metrics["ditto_first_frame_ms"] so realtime first-frame is visible in ops.
rem set DITTO_FIRST_SEG_FRAMES=3

set COQUI_TOS_AGREED=1

rem -- VCam canvas orientation. Portrait 720x1280 = vertical livestream (Douyin/Kuaishou),
rem    real half-body video fills the frame (FIT=cover => no black bars). Comment these 3
rem    lines to revert to landscape 1280x720. Only the vcam broadcast (7870) reads them.
set VCAM_WIDTH=720
set VCAM_HEIGHT=1280
set VCAM_FIT=cover

rem  VCam HEADLESS broadcast (WebRTC/fanout only, no OBS Virtual Camera device).
rem  VCAM_NO_OBS=1 runs the frame loop through an in-proc null-sink instead of pyvirtualcam,
rem  so vcam needs NO OBS install and does NOT grab the OBS Virtual Camera -- i.e. it can
rem  coexist with realtime_stream/faceswap (removes the device-fight noted below) and suits
rem  cloud / phone-viewer (pure-WebRTC) deploys. cam_ready still goes true; /snapshot + WebRTC
rem  + RTMP/record all keep working (they read _latest_rgb, not the OBS device). Default 0 = OBS.
rem  Isolated first-frame forensics 2026-07-02 (headless 7871 + aiortc peer): WebRTC connect
rem  first-frame 157ms (signaling 65ms + ICE/DTLS/keyframe ~92ms, loopback), steady pacing
rem  mean 39.8ms @25fps. NOTE: vcam VIDEO first-frame is immediate on clip dequeue; PREBUF/
rem  AUDIO_PREROLL delay only AUDIO (lip-lead A/V sync), so they are NOT a video first-frame
rem  lever -- end-to-end first-frame is bounded by the ditto DiT render (~0.5s), not vcam.
rem set VCAM_NO_OBS=1
rem  VCam listen port (default 7870). Handy to run a second isolated instance (e.g. 7871) for
rem  measurement without touching the live 7870 broadcast the Hub targets.
rem set VCAM_PORT=7870

rem ==========================================
rem  Hub process supervisor (service_supervisor) trimming for the local
rem  real-camera live-faceswap mode:
rem    - lipsync is unused (mouth comes from the real camera, not a
rem      TTS-driven static avatar)
rem    - vcam_server and realtime_stream would fight over the same OBS
rem      Virtual Camera output and crash, so both are excluded from the
rem      supervisor by default (avoids start-fail -> relaunch storm).
rem  To switch back to static-avatar + lip-sync mode, set the two items
rem  below back to 1.
if not defined HUB_SUP_LIPSYNC set "HUB_SUP_LIPSYNC=0"
if not defined HUB_SUP_VCAM set "HUB_SUP_VCAM=0"
rem  Ditto realtime full-face HD lipsync (external repo C:\ditto). HUB_SUP_DITTO=1 makes the
rem  in-Hub supervisor keep it alive (self-heal) and lets GPU engage(all) start it, so the
rem  realtime-HD default (AVATARHUB_LIPSYNC_RT_DEFAULT=ditto) is always-on. Default 0 = on-demand
rem  (start_ditto.bat); GPU release stops it either way. deploy.env.bat can override.
if not defined HUB_SUP_DITTO set "HUB_SUP_DITTO=0"
rem  EchoMimic full-face HD (offline, external repo C:\echomimic). Offline 60-120s cold load, so
rem  it is NOT self-healed / NOT auto-started by engage(all) -- start on demand via
rem  POST /api/engine/start?name=echomimic and free via /api/engine/stop. GPU release stops it too.
rem  Leave HUB_SUP_ECHOMIMIC=0 (default); =1 only adds (pointless) self-heal for symmetry with ditto.
if not defined HUB_SUP_ECHOMIMIC set "HUB_SUP_ECHOMIMIC=0"
rem  Realtime lipsync LOAD-GATE: when ditto is the realtime default (AVATARHUB_LIPSYNC_RT_DEFAULT=ditto),
rem  a single card under full load makes ditto's 512 render cross real-time (A/B 2026-06-30: idle RTF~0.91,
rem  fish-TTS contention -> 1.08). =1 (default) auto-falls new realtime sessions back to musetalk(256,raw)
rem  when VRAM hits the gate level so we never drop frames; no-op unless ditto is the RT default.
rem  Tune: _VRAM=high|med|off (sensitivity), _UTIL=N (>0 adds a GPU-util%% gate; off by default to avoid
rem  false trips from our own in-flight renders). Set AVATARHUB_RT_LOADGATE=0 to disable entirely.
if not defined AVATARHUB_RT_LOADGATE set "AVATARHUB_RT_LOADGATE=1"
if not defined AVATARHUB_RT_LOADGATE_VRAM set "AVATARHUB_RT_LOADGATE_VRAM=high"
if not defined AVATARHUB_RT_LOADGATE_UTIL set "AVATARHUB_RT_LOADGATE_UTIL=0"

rem  Realtime FACE-SWAP load-adaptive quality (realtime_stream.py). Sibling of the lipsync load-gate above,
rem  but driven by measured per-frame swap latency (EWMA of faceswap_api round-trip): when latency stays high
rem  it auto-steps the preset DOWN (hd->beauty->natural->eco: lower PROC_W + drop GFPGAN, directly cutting
rem  latency) to never drop frames, and steps back UP toward the user-chosen target when latency is comfortable.
rem  Never exceeds the target preset; hysteresis (down/up use different thresholds) + dwell (N consecutive ~2s
rem  samples) avoid flapping. CPU fallback needs no special-case: CPU inference -> high latency -> auto floors to
rem  eco. Set SWAP_AUTO_QUALITY=0 to disable (byte-identical legacy behavior).
rem  [2026-07-05 CALIBRATED, unattended synth-cam rig] thresholds measured on the PRODUCTION route
rem  (realtime->hub:9000->.104 faceswap, 3 in-flight like SWAP_WORKERS). After the enhance-fastpath
rem  (kps-reuse + GFPGAN fp16, see faceswap_api FACESWAP_ENH_*): eco/natural wall mean ~120-140ms,
rem  beauty/hd(gfpgan) ~410-435ms => DOWN=1.5x hd-mean~650, UP=1.15x max(below-target mean)~475 (UP must
rem  sit ABOVE beauty-mean or the climb back to hd stalls mid-ladder). Local-5090 direct is far lower, so
rem  650/475 only ever trips on real overload there too. Live drill passed: overload => steps to eco
rem  (multi-step), load lifted => climbs back to hd. Climb exponential backoff (SWAP_AUTO_CLIMB_HOLDOFF_S,
rem  doubles up to 4x when a climb gets knocked back within 45s, resets after 120s calm) stops
rem  probe-flapping under sustained overload. Recalibrate after GPU/model/preset/enhance changes:
rem  python tools\swap_calibrate.py --n 45 --workers 3 [--direct]
if not defined SWAP_AUTO_QUALITY set "SWAP_AUTO_QUALITY=1"
if not defined SWAP_AUTO_DOWN_MS set "SWAP_AUTO_DOWN_MS=650"
if not defined SWAP_AUTO_UP_MS set "SWAP_AUTO_UP_MS=475"
if not defined SWAP_AUTO_DWELL set "SWAP_AUTO_DWELL=3"
if not defined SWAP_AUTO_CLIMB_HOLDOFF_S set "SWAP_AUTO_CLIMB_HOLDOFF_S=20"
rem  Face-swap PERFORMANCE TREND (cross-time, mirrors interpreter's cross-session TM stats): every SWAP_STATS_EVERY
rem  seconds snapshot {fps/latency/win ok+fail/effective preset/engine cpu_only} into logs/swap_stats.json (rolling,
rem  reloaded on restart for a continuous trend). /ops swap card shows near-window avg fps/latency + recommended
rem  start preset + "fell to CPU N times". SWAP_STATS=0 disables (zero overhead). SWAP_AUTO_REMEMBER=1 (opt-in, needs
rem  SWAP_AUTO_QUALITY=1) starts next run at the remembered sustainable preset (trend mode) to avoid startup drops.
if not defined SWAP_STATS set "SWAP_STATS=1"
if not defined SWAP_AUTO_REMEMBER set "SWAP_AUTO_REMEMBER=0"
rem  [2026-07-05 P0 clarity] Default face-swap TARGET tier = hd (512px + GFPGAN fastpath enhance).
rem  Decision basis: enhance fastpath cut GFPGAN cost 65%%; calibrated production route (.104, 3 workers)
rem  hd wall-mean ~430ms sits inside the 650ms DOWN budget, and SWAP_AUTO_QUALITY=1 still floats
rem  eco..hd under real overload, so "default hd" never trades away liveness. UI preset buttons can
rem  override per-launch via --swap-preset (one_click_start swap_preset).
if not defined SWAP_PRESET set "SWAP_PRESET=hd"
rem  Output(MJPEG preview/phone) JPEG quality, DECOUPLED from the send-to-engine compression (which
rem  follows the tier table: hd=72/natural=55/...). Output encode is local CPU + LAN, ~free; 85 kills
rem  the visible second-compression mush on preview/phone. OBS path (pyvirtualcam raw frames) unaffected.
if not defined SWAP_OUT_JPEG_Q set "SWAP_OUT_JPEG_Q=85"
rem  Engine-health probe for realtime_stream (/ops engine truth + cpu_only auto-floor + UI tier chip):
rem  must point at the PRODUCTION engine (SVC_FACESWAP=.104), not the historical localhost:8000 default.
if not defined FACESWAP_HEALTH_URL set "FACESWAP_HEALTH_URL=http://192.168.0.104:8000/health"
rem  [2026-07-06 P2] Face-region native-resolution channel: crop the ORIGINAL frame by the last
rem  returned face box and send that (face at native pixels, background untouched), instead of
rem  downscaling the whole frame to SWAP_PROC_W. Auto-falls back to full-frame on face loss/multi-face;
rem  periodic full-frame probe (3s) rediscovers extra faces. Runtime toggle: /realtime/swap/crop?on=0|1.
if not defined SWAP_CROP set "SWAP_CROP=1"
rem  [2026-07-06 P4] Enhance concurrency pool: per-slot FaceRestoreHelper (isolated mutable
rem  state), shared GFPGAN/CodeFormer weights. Aligned with SWAP_WORKERS=3, removes the
rem  _enhance_lock serial bottleneck (measured GFPGAN 3-way: 5.3 -> ~15fps class).
rem  concurrency=1 keeps the legacy lock path (zero regression). 4070 12G holds 3; weak cards 1-2.
if not defined FACESWAP_ENH_CONCURRENCY set "FACESWAP_ENH_CONCURRENCY=3"

rem  Cross-machine VRAM mutual-exclusion (peer GPU lease): another LAN box (B) wraps its GPU program
rem  with gpu_peer_lease.py, which POSTs /api/gpu/lease/acquire to make THIS hub (A) clear ALL VRAM
rem  (stop GPU services + unload LLM) for the lease, then /lease/release to restore. Heartbeat keeps the
rem  lease alive; if B crashes, A auto-reclaims after TTL (never stays down). REQUIRES AVATARHUB_API_TOKEN
rem  set (see below) so the remote write calls authenticate. TTL = heartbeat lease seconds.
if not defined AVATARHUB_PEER_LEASE_TTL set "AVATARHUB_PEER_LEASE_TTL=30"
rem  Allow a peer to PREEMPT an active livestream (B passes force=1). Default 0 = never interrupt a live
rem  stream; B just waits/retries until A is idle. Set 1 only if peer work must override live.
if not defined AVATARHUB_PEER_ALLOW_FORCE set "AVATARHUB_PEER_ALLOW_FORCE=0"

rem Cloud API keys are injected from a separate secrets.bat (kept out of shared/backup configs).
if exist "%~dp0secrets.bat" call "%~dp0secrets.bat"

rem License enforcement rollout switch:
rem   0 = evaluation mode (show license state, do not block capabilities) -- safe default
rem   1 = commercial enforcement (invalid/expired license limits HD/multi-session/watermark-free)
if not defined AVATARHUB_LICENSE_ENFORCE set "AVATARHUB_LICENSE_ENFORCE=0"

rem ==========================================
rem  Multi-GPU offload: service URLs are discovered by SSH host-key identity.
rem  Fast path uses cached/last-known hosts; if they are unreachable, service_discovery.py
rem  scans the local /24 and recognizes machines by ED25519 host key fingerprints.
rem  Set AVATARHUB_NO_SERVICE_DISCOVERY=1 to skip discovery and use the fallback URLs below.
rem [5-box cluster] Fixed SVC_ addresses below; discovery OFF to avoid scan overwrite / drift.
set "AVATARHUB_NO_SERVICE_DISCOVERY=1"
if not defined AVATARHUB_NO_SERVICE_DISCOVERY (
  set "DISCOVERY_PY=%FACEFUSION_PY%"
  if not exist "%DISCOVERY_PY%" set "DISCOVERY_PY=python"
  for /f "usebackq delims=" %%L in (`"%DISCOVERY_PY%" "%~dp0service_discovery.py" --emit-bat 2^>nul`) do call %%L
)
rem ========== Fixed service addresses (5-box LAN cluster 192.168.0.x wired) ==========
rem   .176=5090 brain (LLM+hub+interpreter+vcam+clone TTS) ; .104=faceswap+lipsync ; .140=STT ; .198/.117=standby/emotion/singing
rem [cluster] STT -> .140 (RTX4070, faster-whisper large-v3-turbo). No local STT; VRAM goes to the 32B LLM.
rem   To move STT back local: comment the next line (launcher auto-starts local STT).
if not defined SVC_STT set "SVC_STT=http://192.168.0.140:7854"
rem [cluster] Nemotron streaming STT -> .140 (moved off 5090 on 2026-07-05, frees 5.6G for LLM slot).
rem   Shares the .140 card with whisper: bf16 4.3G + whisper 4.4G = 8.7G/12G; interp also unloads
rem   whisper while streaming, so headroom is larger in practice. Remote side pins bf16 + graphs-off
rem   in D:\faceX\start_nemo_stt.bat (NEMO_* vars below do NOT affect the remote service).
rem   Roll back to local: comment the next two lines + restore the nemo line in boot_stack.bat.
if not defined SVC_NEMO_STT set "SVC_NEMO_STT=http://192.168.0.140:7857"
if not defined INTERP_NEMO_WS set "INTERP_NEMO_WS=ws://192.168.0.140:7857"
rem [cluster] Clone TTS stays on 5090: measured Fish 8.7s on 4070 vs 1.7s on 5090 (torch.compile);
rem   TTS bursts never fight the LLM for the card, so moving it off is a net loss.
rem [cluster] Faceswap -> .104 (RTX4070, facefusion warm ~250ms/swap20ms). Old .1.43 kept below for reference.
rem if not defined SVC_FACESWAP set "SVC_FACESWAP=http://192.168.1.43:8000"
if not defined SVC_FACESWAP set "SVC_FACESWAP=http://192.168.0.104:8000"
rem [cluster] Lipsync back on 5090: measured 5090 streaming STD 0.44x / HD 0.67x realtime (ttfv~300ms),
rem   far ahead of the 4070 (.198 at 1.0x edge / HD stutter). After clearing the STT zombie (moved
rem   to .140) + DITTO overlap, ~8G freed: 32B + Fish + lipsync all resident on 5090 under 32G.
rem   Old routes (lipsync->.198:8090 ; lipsync+faceswap both on .104) kept below for reference.
rem [standby] .198 = lipsync hot-standby (verified 2026-07-04: cold start ~2min model load).
rem   Normally NOT running (zero VRAM). Revive: ssh Administrator@192.168.0.198 then run
rem   D:\faceX\start_lipsync_standby.bat (or remote Win32_Process.Create).
rem   Enable: uncomment next line (or hot-PATCH /api/config {"services":{"lipsync":"http://192.168.0.198:8090"}}).
rem P4 2026-07-09 cluster re-plan: lipsync(digital-human) pinned to .198 replica (local copy parked to keep
rem 5090 VRAM for call-core: faceswap-stream + emotion-TTS + GER-LLM). Start _launch_lipsync_local.bat for burst.
if not defined SVC_LIPSYNC set "SVC_LIPSYNC=http://127.0.0.1:8090,http://192.168.0.198:8090"
rem [cluster] VCAM_URL preset to the LAN address so a REMOTE lipsync node (e.g. spill to .198)
rem   can stream straight to the broadcast hub; 127.0.0.1 also works for local lipsync but the
rem   LAN IP is harmless there and safer for remote replicas. Restart Hub after changing.
if not defined VCAM_URL set "VCAM_URL=http://192.168.0.176:7870"
rem [cluster] Emotion TTS -> .117 (RTX3060, CosyVoice3-0.5B, conda-pack deploy 2026-07-04). Old .1.43 kept for reference.
if not defined SVC_EMOTION_TTS set "SVC_EMOTION_TTS=http://192.168.0.117:7852"
rem [cluster] Qwen3-TTS 0.6B -> .117 (Apache-2.0 commercial fallback engine, landed 2026-07-05).
rem   A/B measured (3060, 4 threads): RTF~2.8, below realtime - NOT the default engine; offline
rem   render / license-fallback only. Profile tts_engine=qwen3_tts routes here; realtime stays fish (CONV_TTS_ENGINE).
if not defined SVC_QWEN3_TTS set "SVC_QWEN3_TTS=http://192.168.0.117:7858"

rem ========== Interpreter noise gate (calibrated to THIS room, 2026-07-04) ==========
rem Gate history: BRIO webcam mic measured room floor RMS -44~-49dBFS (fans), so gates were
rem raised to -40/-32 to block it. Mic is now PD100X (dynamic, close-talk): measured floor
rem RMS -71dBFS, but real speech at normal desk distance measured as low as rms -44.3, so
rem the BRIO-era -40 gate ate it (a real name utterance dropped 2026-07-07 17:40). Restored defaults
rem -50/-41: still 20dB+ above PD100X floor, no longer kills normal-volume speech.
if not defined INTERP_GATE_RMS_DBFS set "INTERP_GATE_RMS_DBFS=-50"
if not defined INTERP_GATE_PEAK_DBFS set "INTERP_GATE_PEAK_DBFS=-41"
rem Dyn gate 8 -> 5: PD100X close-mic speech is dense; measured a real sentence at dyn=6.9
rem getting dropped by the 8dB gate (tuned for BRIO fan hum, now redundant with rms -40 gate).
rem Target noise measures dyn 2~4, so 5 keeps full margin without eating real speech.
if not defined INTERP_GATE_DYN set "INTERP_GATE_DYN=5"
rem Min utterance 0.4 -> 0.7s: chair/keyboard impulse bursts (high dyn, gate can't stop) are too short to be a sentence - never sent to ASR.
if not defined INTERP_VAD_MIN set "INTERP_VAD_MIN=0.7"

rem ========== P1 interpreter four-pack (live 2026-07-04) ==========
rem Voice-lock: only the ENROLLED speaker gets translated (bystander/TV/keyboard voiceprints are
rem far in embedding distance -> all blocked). Enroll = first 3 fully-gated real sentences after
rem start (zero-touch), or POST /voicelock/enroll for an explicit 6s take. Model = CosyVoice's
rem campplus.onnx (CPU 10-70ms/segment). Measured same-person similarity 0.73+ / other 0.41-, gate 0.52 has margin.
if not defined INTERP_VOICELOCK set "INTERP_VOICELOCK=1"
if not defined INTERP_VOICELOCK_THR set "INTERP_VOICELOCK_THR=0.52"
if not defined INTERP_VOICELOCK_AUTOENROLL set "INTERP_VOICELOCK_AUTOENROLL=1"
rem Denoise front-end: spectral subtraction washes steady noise (fan/AC; CPU ~16ms per 2s, zero VRAM) before ASR. Better accuracy, fewer hallucinations.
if not defined INTERP_DENOISE set "INTERP_DENOISE=1"
if not defined INTERP_DENOISE_PROP set "INTERP_DENOISE_PROP=0.75"
rem Monitor (ear-return): mirror the clone voice at low volume to the default output (your headphones) to confirm what the peer hears. Default OFF; page headphone button toggles anytime.
if not defined INTERP_MONITOR set "INTERP_MONITOR=0"
if not defined INTERP_MONITOR_GAIN set "INTERP_MONITOR_GAIN=0.25"
rem Speaker half-duplex (2026-07-04): while monitor/readback plays out loud, feed silence to
rem capture upstream. Prevents speaker bleed-in (garbled ASR + VAD never finalizing).
rem Set 0 only if always wearing headphones (restores full-duplex talk-while-playing).
if not defined INTERP_HALF_DUPLEX set "INTERP_HALF_DUPLEX=1"
rem Self-output text echo gate window/similarity (drop re-captured own dubbing by text match).
if not defined INTERP_SELF_ECHO_WINDOW_S set "INTERP_SELF_ECHO_WINDOW_S=25"
if not defined INTERP_SELF_ECHO_SIM set "INTERP_SELF_ECHO_SIM=0.75"
rem P8-2 acoustic coupling probe (call-start chirp test): margin over noise floor to call
rem "speaker<->mic coupled". Both bands >= INTERP_COUPLE_DB, or one band >= _HI -> coupled
rem (asymmetric on purpose: false "coupled" = harmless turn-taking; false "isolated" = echo).
if not defined INTERP_COUPLE_DB set "INTERP_COUPLE_DB=10"
if not defined INTERP_COUPLE_DB_HI set "INTERP_COUPLE_DB_HI=18"
rem One-click call mode "my mic" binds BY NAME (device indexes drift across reboots, names do not).
if not defined INTERP_MIC_NAME set "INTERP_MIC_NAME=PD100X"

rem ========== P2 experience loop (live 2026-07-04) ==========
rem Peer readback: peer's foreign speech -> Chinese translation read aloud INTO YOUR HEADPHONES
rem using the PEER'S OWN cloned voice (no subtitle-staring). Reference audio = peer's first fully
rem gated utterance >=2.5s (auto-captured, reused all session, Fish ref cache hits throughout).
rem Default OFF; page readback button toggles anytime. WEAR HEADPHONES (speaker playout would be
rem re-captured by the mic; voice-lock blocks it but wastes gating).
if not defined INTERP_READBACK set "INTERP_READBACK=0"
if not defined INTERP_READBACK_GAIN set "INTERP_READBACK_GAIN=0.9"
rem Nemotron streaming word-level STT (7857): the real-speech CUDA crash culprit is the RNNT
rem CUDA-Graph decode kernel (unstable on a shared card); server defaults graphs OFF
rem (NEMO_CUDA_GRAPHS=0) -> stable at full precision. Precision default fp32 (accuracy first;
rem bf16 saves 1.3G but differs on the noisiest sample). VRAM ~5.6G after expandable_segments settles.
rem NOTE 2026-07-05: service now runs on .140 (bf16 pinned in its remote start bat);
rem the two vars below only matter for an emergency local fallback.
if not defined NEMO_PRECISION set "NEMO_PRECISION=fp32"
if not defined NEMO_CUDA_GRAPHS set "NEMO_CUDA_GRAPHS=0"

rem ========== P3 (2026-07-04): field tuning + RNNoise denoise ==========
rem Denoise engine: rnnoise = frame-level streaming (10ms, CPU 4% realtime; also crushes
rem non-stationary noise like keyboards; washes BOTH streaming and segmented paths).
rem nr = legacy spectral subtraction (segmented path only). Missing lib auto-falls back to spectral. Zero risk.
if not defined INTERP_DENOISE_ENGINE set "INTERP_DENOISE_ENGINE=rnnoise"
rem Field tuning (volume / echo gate / voiceprint threshold etc) persists in data\tuning.json,
rem which OVERRIDES the defaults in this file; edit via the interpreter Tune panel or POST /tune, reset via POST /tune/reset.

rem ========== P0 accuracy loop (2026-07-07): GER final-pass correction + ASR hotwords + gate mark-mode ==========
rem GER: after each streaming final is shown/dubbed, ASYNC re-transcribe the same audio with
rem whisper large-v3-turbo (heterogeneous 2nd hypothesis), arbitrate homophone errors with the
rem local LLM (glossary-constrained), verify via pinyin-similarity gate (corrections may only
rem swap same/similar-sounding chars), then replace the on-screen subtitle. Dub already played:
rem zero added live latency. Mark-mode: recoverable gate rejections (noise-floor / hallucination
rem / weak filler / language drift) now show gray pending text + same review; real speech gets
rem promoted (translated + late dub - beats silent loss), noise gets retracted. Echo / voiceprint
rem / dup-loop blocks stay hard (promoting those = feedback loop or privacy leak).
if not defined INTERP_GER set "INTERP_GER=1"
if not defined INTERP_GER_SUSPECT set "INTERP_GER_SUSPECT=1"
rem Glossary terms -> whisper initial_prompt (biases decoding toward canonical spellings of names/brands/jargon).
if not defined INTERP_ASR_HOTWORDS set "INTERP_ASR_HOTWORDS=1"
rem Pinyin gate: min pronunciation similarity between corrected and original (rejects LLM rewrites).
if not defined INTERP_GER_PY_SIM set "INTERP_GER_PY_SIM=0.62"
rem P1 fast path: if the turbo re-transcription is pronunciation-identical (>= this) to the
rem streaming final, it is a pure homophone dispute -> adopt turbo directly, skip the LLM
rem arbitration round-trip (saves 1~3.5s per correction). Below threshold = engines truly
rem disagree -> still LLM-arbitrated. 1.1 disables the fast path.
if not defined INTERP_GER_TRUST_SIM set "INTERP_GER_TRUST_SIM=0.85"
rem P4 truncation recovery: quiet speech makes the streaming engine drop half the sentence
rem (bench: 28% CER at -24dB vs turbo 4%). If the turbo re-transcription is clearly longer and
rem phonetically CONTAINS the streaming final (>=80% coverage) with high confidence, adopt the
rem full turbo sentence. INTERP_GER_TRUNC=0 disables.
if not defined INTERP_GER_TRUNC set "INTERP_GER_TRUNC=1"
rem P1 self-learning: recurring GER fixes (same wrong->right pair seen N times) auto-adopt the
rem correct spelling into glossary '*' as identity term -> becomes an ASR hotword next utterance,
rem attacking the recurring misrecognition at the source. Ledger: data/ger_learned.json,
rem inspect via GET /ger/learned. Set INTERP_GER_LEARN_ADOPT=0 to record only (no auto-adopt).
if not defined INTERP_GER_LEARN set "INTERP_GER_LEARN=1"
if not defined INTERP_GER_LEARN_ADOPT set "INTERP_GER_LEARN_ADOPT=2"
rem P3 review capture: per-sentence audio clips kept for the /review page (playback + human
rem right/wrong marking -> true CER trend). Audio dirs auto-pruned to last 3 sessions, max
rem INTERP_REVIEW_MAX clips/session, 12s/clip. INTERP_REVIEW=0 disables capture entirely.
if not defined INTERP_REVIEW set "INTERP_REVIEW=1"
if not defined INTERP_REVIEW_MAX set "INTERP_REVIEW_MAX=40"

rem ========== [Plan B] Local LLM interpreter translation + Whisper turbo (fill the 32G card) ==========
rem Local STT: faster-whisper large-v3-turbo (several times faster than large-v3, ~same quality, less VRAM). Revert: STT_MODEL=large-v3
if not defined STT_MODEL set "STT_MODEL=large-v3-turbo"
rem MT backend: auto = local LLM (ollama) first, auto-falls back opus-mt -> Google; local = pure legacy opus-mt (zero regression).
rem 2026-07-09 call-latency fix: llm layer measured p90 3.6s/sentence on the contended local card; local NMT ~90ms.
rem Dub uses the first-pass translation, so "local" cuts spoken latency by seconds (GER still polishes subtitles async).
if not defined INTERP_MT_BACKEND set "INTERP_MT_BACKEND=local"
rem 2026-07-09: EOU silence threshold 500-˃400ms (faster finalize; below ~350 risks splitting sentences).
if not defined INTERP_STREAM_SIL_MS set "INTERP_STREAM_SIL_MS=400"
rem 2026-07-09: qwen3(.117) measured 0/5 short-sentence repeats vs fish 2-4/5; per-sentence auto-fallback to fish stays.
rem 2026-07-10 live test: qwen3(.117 3060) ja whole-sentence synth piled 15-21s tails on monologues;
rem local cosyvoice streams on the 5090 (interim until qwen3 is localized). Pronunciation tradeoff accepted in test.
rem 2026-07-11: base engine set to fish for real-time snappiness (fish warm ~0.7-1.1s/sentence vs cosyvoice
rem remote .117 ~4s). Strong-emotion sentences still auto-detour to CosyVoice3 (_synth_emotional, INTERP_EMO_TTS=1),
rem so emotion is preserved only where it matters. Switch back to cosyvoice here if you prefer emotion on every line.
if not defined INTERP_TTS_ENGINE set "INTERP_TTS_ENGINE=fish"
rem 2026-07-09 P0 emotion routing: high-arousal sentences (excited/angry/sad/surprised) detour to
rem CosyVoice3 clone+instruct on .117 (measured 2-4s/sentence, real prosody shift); neutral stays qwen3 stream.
if not defined INTERP_EMO_TTS set "INTERP_EMO_TTS=1"
rem P1 2026-07-09: mood-from-chat-history (local LLM, ~315ms async, throttled) + loudness corroboration.
if not defined INTERP_EMO_LLM set "INTERP_EMO_LLM=1"
rem Emotional-sentence speaking rate for CosyVoice3 (user asked for faster speech).
if not defined INTERP_EMO_SPEED set "INTERP_EMO_SPEED=1.1"
rem P5 2026-07-10: colloquial LLM translation for these DST langs even with MT_BACKEND=local (7b warm ~180ms,
rem adds natural particles); emotion interjection prefixes + [laughter] tags on emotional sentences.
if not defined INTERP_MT_LLM_LANGS set "INTERP_MT_LLM_LANGS=ja"
if not defined INTERP_EMO_WORDS set "INTERP_EMO_WORDS=1"
rem P3a 2026-07-09: emotional TTS moved to local 5090 (freed ditto+rvc VRAM; measured first-chunk 3-4s -> 1.7-2.5s).
rem .117 emotion server stays warm as manual fallback - delete this line to route back.
if not defined COSYVOICE_TTS_URL set "COSYVOICE_TTS_URL=http://127.0.0.1:7852"
rem P5d 2026-07-10: 林小玲东京女声 SBV2 JP-Extra 五情绪模型；ja 语向自动路由(训练完成后生效)。
rem SBV2 是按人设训练的音色，只在白名单人设(默认林小玲)激活；其他人设日语仍走克隆链路。
if not defined INTERP_JA_TTS_ENGINE set "INTERP_JA_TTS_ENGINE=sbv2"
rem P7 2026-07-10: ambient comfort-noise bed - loops the user's real room tone under/between
rem synthesized sentences (kills the "background suddenly dies -> obviously fake" tell).
rem Captured only from non-speech gated blocks (never leaks source speech). Runtime: POST /config/ambient.
if not defined INTERP_AMBIENT_BED set "INTERP_AMBIENT_BED=1"
if not defined INTERP_AMBIENT_GAIN set "INTERP_AMBIENT_GAIN=1.0"
if not defined INTERP_AMBIENT_MAX_DBFS set "INTERP_AMBIENT_MAX_DBFS=-38"
if not defined INTERP_SBV2_PROFILES set "INTERP_SBV2_PROFILES=林小玲"
if not defined SBV2_TTS_URL set "SBV2_TTS_URL=http://127.0.0.1:7861"
rem 2026-07-10 voice-guard: profiles without a voice sample now SKIP dubbing with a visible warning
rem (was: every sentence hit cosyvoice/fish with 400/500, user only saw "no sound").
rem 2026-07-14 fresh-install default: interpreter auto-falls-back to the bundled starter voice
rem (data\starter_profiles\voices\starter_zh_f.wav) so a sample-less profile is not silent.
rem Override with your own wav (same-name .txt = reference text), or disable with =off:
rem set "INTERP_FALLBACK_VOICE=refs\interp_林小玲.wav"
rem set "INTERP_FALLBACK_VOICE=off"
if not defined SVC_SBV2_TTS set "SVC_SBV2_TTS=http://127.0.0.1:7861"
rem P11 2026-07-10: OpenAudio S1-mini 日语混合路由(真笑/哭腔)。LoRA 完成前保持 sbv2。
if not defined INTERP_JA_TTS_MODE set "INTERP_JA_TTS_MODE=sbv2"
if not defined S1_TTS_URL set "S1_TTS_URL=http://127.0.0.1:7863"
if not defined S1_UPSTREAM set "S1_UPSTREAM=http://127.0.0.1:7862"
rem LoRA merge 完成后设此路径并改 INTERP_JA_TTS_MODE=hybrid 启用混合路由:
rem set "S1_LORA_PATH=C:\fishs1\checkpoints\openaudio-s1-mini-lx"
rem MT LLM (ollama tag). qwen2.5:32b works today; once qwen3:32b is pulled+verified, switch the tag to go max.
if not defined INTERP_LLM_MODEL set "INTERP_LLM_MODEL=qwen2.5:32b"
rem Per-sentence context cap: 2048 shrinks the 32B KV cache ~11G -> ~0.6G (nearly all on GPU; latency 5-7s back to ~2s).
if not defined INTERP_LLM_NUM_CTX set "INTERP_LLM_NUM_CTX=2048"
rem Survival baselines are tagged per backend (offline compare per-direction term survival across model swaps).
if not defined INTERP_MT_TAG set "INTERP_MT_TAG=llm:qwen2.5:32b"
rem LLM ~2s/sentence: session-start TM warmup (dozens of sentences) would fight the live stream for GPU -> OFF (cache still fills as you speak; hits are 0ms).
if not defined INTERP_TM_WARMUP set "INTERP_TM_WARMUP=0"

rem  Interpreter GLOSSARY (term-locking): forces brand/person/proper-noun/jargon terms to a fixed
rem  translation (kills MT transliteration drift / mistranslation, critical for live interpretation).
rem  Edit data\glossary.json (hot-reloaded on mtime, or POST /config/glossary/reload). Empty table =
rem  no effect (zero regression). INTERP_GLOSSARY=0 hard-disables even a populated table.
if not defined INTERP_GLOSSARY set "INTERP_GLOSSARY=1"
rem set INTERP_GLOSSARY_PATH=%~dp0data\glossary.json
rem  Glossary placeholder SURVIVAL self-check: per-language-pair, track how often term placeholders
rem  survive the MT round-trip (measured opus-mt zh->en = 100%%). If a pair drops below WARN with
rem  enough samples, /ops flags it (that pair's term-locking is being shredded by MT -> auto-heals to
rem  unprotected translation = no garbage, but term not locked). MIN = min placeholders before judging.
if not defined INTERP_GLOSSARY_SURVIVAL set "INTERP_GLOSSARY_SURVIVAL=1"
if not defined INTERP_GLOSSARY_SURVIVAL_MIN set "INTERP_GLOSSARY_SURVIVAL_MIN=20"
if not defined INTERP_GLOSSARY_SURVIVAL_WARN set "INTERP_GLOSSARY_SURVIVAL_WARN=0.85"
rem  REGRESS = on-demand probe flags a pair with "regression" when its survival drops by >= this vs the
rem  last baseline for the SAME backend (logs/gloss_survival.jsonl). Catches silent drift even while both
rem  runs stay above WARN (e.g. 100%% -> 88%%). Shown as a note in /ops probe + acceptance gate scope (no FAIL).
if not defined INTERP_GLOSSARY_SURVIVAL_REGRESS set "INTERP_GLOSSARY_SURVIVAL_REGRESS=0.05"
rem  SCHEDULED PROBE (unattended drift watch): PROBE_EVERY = minutes between auto-probes (0 = OFF, default).
rem  Set e.g. 30 to have the interpreter self-run the survival probe every 30 min; a regression vs the last
rem  same-backend baseline fires a one-shot event to alerts.py (webhook/DingTalk/toast). PROBE_IDLE = only
rem  run a scheduled probe after this many seconds with NO translation (so it never steals MT mid-broadcast).
if not defined INTERP_GLOSSARY_PROBE_EVERY set "INTERP_GLOSSARY_PROBE_EVERY=0"
if not defined INTERP_GLOSSARY_PROBE_IDLE set "INTERP_GLOSSARY_PROBE_IDLE=20"
rem  Interpreter TRANSLATION CACHE (translation memory): identical (direction + text) returns the
rem  cached result -> lower latency for repeats + consistent output (same sentence always same
rem  translation, key for professional interpreting) + less MT load. Cache key embeds the glossary
rem  signature, so editing terms auto-invalidates stale entries. Only real translations are cached
rem  (never the echo-back when MT is fully down). =0 disables (pure passthrough, zero regression).
if not defined INTERP_TRANSLATE_CACHE set "INTERP_TRANSLATE_CACHE=1"
if not defined INTERP_TRANSLATE_CACHE_SIZE set "INTERP_TRANSLATE_CACHE_SIZE=512"
rem  Interpreter TRANSCRIPT capture/export: each finalized turn records source+translation+relative
rem  timestamp; export via /transcript.txt (with times), /transcript.srt (subtitle file for a recorded
rem  video/edit), /transcript.json. On session end it is saved to logs/interp_transcript_*.json for
rem  later export (?session=<stamp>). Key for meetings/interpreting (minutes) and content (burn subs
rem  onto recordings). =0 disables capture (export empty, zero overhead). _MAX caps in-memory rows.
if not defined INTERP_TRANSCRIPT set "INTERP_TRANSCRIPT=1"
if not defined INTERP_TRANSCRIPT_MAX set "INTERP_TRANSCRIPT_MAX=5000"
rem  Interpreter TM WARMUP: at session start, background pre-translate high-frequency phrases
rem  (greetings/thanks/backchannels/openers from data/warmup.json) into the translation cache, so
rem  their FIRST real occurrence is an instant cache hit instead of paying MT latency. Best-effort
rem  daemon (never blocks the session), delayed a bit so it doesn't fight the first utterance for MT.
rem  Only meaningful when the translate cache is on; cross-session cache means it truly translates
rem  only on the first session. =0 disables. _MAX caps phrases per language (overload guard).
if not defined INTERP_TM_WARMUP set "INTERP_TM_WARMUP=1"
if not defined INTERP_TM_WARMUP_MAX set "INTERP_TM_WARMUP_MAX=200"
rem  TM WARMUP SELF-LEARN: mine recent logs/interp_transcript_*.json for high-frequency short source
rem  phrases and merge them into the warmup set, so warmup adapts to THIS deployment's real vocabulary
rem  (product names, recurring openers, jargon) instead of only the static data/warmup.json. Closes the
rem  loop transcript-retention -> warmup. =0 disables (static list only). _MIN = min recurrences to learn
rem  a phrase; preview via GET /config/tm_warmup/preview.
if not defined INTERP_TM_WARMUP_LEARN set "INTERP_TM_WARMUP_LEARN=1"
if not defined INTERP_TM_WARMUP_LEARN_MIN set "INTERP_TM_WARMUP_LEARN_MIN=3"
rem  TM STATS (cross-session): on each session end, append this session's cache hit-rate + warmup summary
rem  to logs/interp_tm_stats.json (a trend, so you can see whether cache/warmup ROI climbs over sessions),
rem  and accumulate the top short phrases that MISSED the cache (i.e. actually paid MT latency). View via
rem  GET /config/tm_stats (and the /ops translation-quality card). =0 disables (no file, zero overhead).
rem  _FROM_STATS=1 (opt-in) additionally feeds those repeated-miss phrases into warmup (complements the
rem  transcript self-learn with direct "paid MT latency" evidence); default 0 = no behavior change.
if not defined INTERP_TM_STATS set "INTERP_TM_STATS=1"
if not defined INTERP_TM_WARMUP_FROM_STATS set "INTERP_TM_WARMUP_FROM_STATS=0"
rem  P4-5 TM fix for the measured 0% hit-rate: per-session warmup stays OFF (would fight the
rem  32B LLM for GPU mid-call); instead (a) the translate cache is PERSISTED to data/tm_cache.json
rem  (restart no longer wipes it; invalidated as a whole when the glossary file changes) and
rem  (b) warmup runs ONCE at service boot, slow-paced (gap between sentences, auto-yields while
rem  a session is running). Greetings/thanks now hit instantly in every later session.
if not defined INTERP_TM_CACHE_PERSIST set "INTERP_TM_CACHE_PERSIST=1"
if not defined INTERP_TM_WARMUP_BOOT set "INTERP_TM_WARMUP_BOOT=1"
if not defined INTERP_TM_WARMUP_BOOT_DELAY set "INTERP_TM_WARMUP_BOOT_DELAY=45"
if not defined INTERP_TM_WARMUP_BOOT_GAP set "INTERP_TM_WARMUP_BOOT_GAP=0.3"
rem  P4-1 language-pair warm: on /config/langs switch (and session start, remote layer only)
rem  pre-touch BOTH translation layers for the new pair in the background: one short LLM call
rem  plus one direct STT /translate per direction. Reason: the remote Marian/NLLB fallback
rem  lazy-loads per pair (measured ja->zh first call 73s, warm 0.2s) - a mid-call LLM outage
rem  must not run into that cold load. =0 disables.
if not defined INTERP_LANG_WARM set "INTERP_LANG_WARM=1"
rem  P4-2 session quality gate: on session end compare this session against the baseline
rem  (median of last 20 valid sessions) and push an alert (webhook/toast via alerts.py) when
rem  degraded: e2e latency ratio/red-line, drop-rate ratio/red-line, loopback discontinuity
rem  rate. Short smoke sessions (<120s or <5 segments) are never evaluated (no false alarms).
if not defined INTERP_QA_ALERT set "INTERP_QA_ALERT=1"
if not defined INTERP_QA_MIN_DUR_S set "INTERP_QA_MIN_DUR_S=120"
rem  P4-4 loopback capture buffer (ms). soundcard's default WASAPI buffer is the device period
rem  (~10ms): any Python stall over that drops data ("data discontinuity" warnings, potential
rem  lost words in direction B). 500ms is a jitter pool only - record() still pulls 50ms chunks,
rem  so link latency is unchanged. Warnings are folded into a counter (metrics: audio_health.discont).
if not defined INTERP_LOOPBACK_BUF_MS set "INTERP_LOOPBACK_BUF_MS=500"
rem  P5-2/P6-1 weak-language guard. Source of truth is now data\weak_langs.json, generated by
rem  tools\lang_qa.py (dual-ASR review across all languages; rerun after upgrading the streaming
rem  ASR and cleared languages drop off automatically). This env var is a manual ADDITION (union
rem  with the file), keep it empty normally. Comma-separated 2-letter codes.
if not defined INTERP_STREAM_WEAK_LANGS set "INTERP_STREAM_WEAK_LANGS="
rem  P6-5 client-driven preload: at boot the interpreter aggregates the real language-pair
rem  distribution from its session logs and POSTs the top pairs to the .140 /translate/preload
rem  endpoint (the server warms those lazily-loaded models in the background). =0 disables.
if not defined INTERP_PRELOAD_PUSH set "INTERP_PRELOAD_PUSH=1"
rem  P5-5 remote STT (.140) preload: NLLB lazy-load measured 73s on first pair - preload the
rem  model at boot and pre-run the common pairs so the fallback is always hot server-side too.
rem  (Set on the local copy for documentation; the deployed .140 copy reads its own env.)
if not defined STT_PRELOAD_NLLB set "STT_PRELOAD_NLLB=1"
if not defined STT_PRELOAD_PAIRS set "STT_PRELOAD_PAIRS=ja:zh,zh:ja,ko:zh,zh:ko,ru:zh,zh:ru"
rem  Dialogue main-chain TTS engine. BREAKTHROUGH (2026-06-12): fish torch.compile now
rem  works on Windows (short cache dirs C:\tc\C:\ic to beat the 260-char path limit +
rem  plain-inductor mode to dodge the cudagraphs LLP64 overflow), dropping fish from
rem  RTF~1.1 to ~0.22 (~1.5s/sentence, local on 5090, best clone quality). Measured
rem  converse: fish 5 sentences in 7.8s vs CosyVoice(184) 3 sentences in 21s. So the
rem  dialogue main-chain is back on fish. "cosyvoice" = force CosyVoice(184); empty =
rem  respect each profile's tts_engine. CosyVoice stays as emotion/fallback engine.
set CONV_TTS_ENGINE=fish_speech
rem  Future expansion (fill in when those nodes are ready):
rem    set SVC_EMOTION_TTS=http://192.168.0.167:7852  (emotion TTS  -> 4090#1)
rem    set SVC_SINGING=http://192.168.0.184:7853      (singing      -> 4090#2)
rem  Keep lipsync(8090) + vcam broadcast(7870) on the local 5090 ONLY (single copy).

rem ==========================================
rem  Delivery knobs (P-Conc): concurrency / multi-card / audience. All OPTIONAL.
rem  Defaults below = current behavior; uncomment + edit only when scaling out.
rem ==========================================

rem -- Realtime conversation admission (how many parallel routes this box serves) --
rem   0    = OFF (no admission; single creator route. DEFAULT)
rem   auto = K auto-tracks the min HEALTHY replica count across multi-card pools
rem   N    = fixed cap of N parallel routes; extras queue fairly (priority-aware)
rem set CONV_MAX_CONCURRENT=auto
rem   Max queue depth before new requests get a graceful "busy" (0 = unbounded).
rem set CONV_MAX_QUEUE=20
rem   Non-stream request max wait in queue (seconds) before 429.
rem set CONV_WAIT_TIMEOUT=30

rem -- Multi-card worker pools: give a service COMMA-SEPARATED replica URLs and the
rem    hub load-balances (least-inflight) + auto-ejects dead replicas. Single URL =
rem    unchanged single-card behavior. Example: fish on two boxes -->
rem set SVC_FISH_TTS=http://192.168.0.167:7855,http://192.168.0.184:7855
rem set SVC_EMOTION_TTS=http://192.168.0.184:7852,http://192.168.0.185:7852
rem set SVC_LIPSYNC=http://127.0.0.1:8090,http://192.168.0.185:8090
rem   Active health probe interval seconds (0 = off); passive ejection thresholds.
rem set SVC_PROBE_SEC=10
rem set SVC_FAIL_THRESH=2
rem set SVC_FAIL_COOLDOWN=15
rem   Lipsync/TTS pipelining within ONE answer. auto(DEFAULT)=serialize on single-card
rem   (avoids same-GPU contention; identical to old behavior), but auto-PIPELINES when
rem   the lipsync pool has >1 replica (SVC_LIPSYNC above) so a sentence's lipsync runs on
rem   one card while the next sentence's TTS runs on another -> shorter per-answer time.
rem   1=force serialize, 0=force concurrent (use if TTS & lipsync are on separate GPUs).
rem set CONV_LIPSYNC_SERIALIZE=auto
rem   Lipsync-server per-call GC throttle (set in lipsync_server.py env). The finally-block
rem   gc.collect() costs ~188ms PER CALL and was the dominant fixed overhead for streaming small
rem   chunks (measured: per-call fixed cost 256ms -> 51ms after throttling; 0.3s chunk RTF 0.78x
rem   -> 1.58x on single card). Now GC runs only every N rendered frames (default 500 ~= 20s of
rem   video), amortizing it; big frame lists free by refcount anyway, and mem_watchdog calls /gc
rem   as a safety net. Lower it if RSS creeps on very long sessions; 0 = GC every call (old).
rem set LIPSYNC_GC_FRAME_THRESHOLD=500

rem -- Intra-sentence streaming TTS (synthesize a sentence in chunks, feed lipsync per chunk) --
rem   Breaks the "wait for the whole sentence to synthesize before any audio/lip" latency:
rem   the first audio chunk starts playing / driving the mouth while the rest still synthesizes.
rem   Biggest TTFA win on a SINGLE TTS card (where prefetch/multi-card pipelining can't help).
rem   OFF by default (0) -> classic whole-sentence path, byte-for-byte unchanged (zero regression).
rem   Requires fish_speech_server.py with /v1/tts/clone/stream (additive endpoint already shipped)
rem   and a cloned voice profile (voice_b64). Only the fish_speech engine streams; others fall back.
rem   VERIFY ON THE GPU BOX before relying on it (chunk cadence / lip smoothness are hardware-tuned).
rem set CONV_TTS_STREAMING=1
rem   Adaptive chunk coalescing (Hub-side): merge fish's small PCM segments before feeding lipsync.
rem   First block stays small (low TTFA), later blocks larger (smooth mouth); per-block cap bounds it.
rem   Tune on hardware: smaller FIRST_MS = faster first audio but choppier start; bigger CHUNK_MS =
rem   smoother lip but coarser barge-in granularity. 0 = no merge for that tier (emit per fish segment).
rem set CONV_TTS_STREAM_FIRST_MS=300
rem set CONV_TTS_STREAM_CHUNK_MS=550
rem set CONV_TTS_STREAM_MAX_MS=1500
rem   --- Lipsync(video)-mode coalescing: each audio block = ONE MuseTalk call. Single-card bench:
rem   fixed overhead ~256ms/call + 19.7ms/frame (STD, 50fps) ; HD/GFPGAN 28.5ms/frame (35fps).
rem   Tiny blocks -> overhead dominates -> render RTF<1 -> live stream falls behind. So video turns use
rem   BIGGER blocks (auto-selected when generate_lipsync + face). STD: >=1s/block -> ~1.5x realtime.
rem   HD: render margin is thin (~1.27x whole-sentence), so HD uses near-whole-sentence blocks; if HD
rem   live still stutters, switch to STD (no enhance) or add a 2nd lipsync card. Defaults usually fine.
rem   *** HD real-time FIX (2026-06): the old "HD stutters" was NOT inherent cost but cuDNN autotune on
rem   every new frame-count + variable last-batch (first 100-frame call stalled ~20s). face_enhance.py now
rem   (a) pads GFPGAN to a fixed batch (FACE_ENH_BATCH, default 8) and (b) forces cudnn.benchmark=False.
rem   Result: HD now streams steady at ~1.5-1.64x realtime on ONE 5090 (was 0.67-0.83x). HD live is viable.
rem   ENHANCE_DTYPE=bf16 (default) ; set fp32 only if a card lacks bf16. FACE_ENH_BATCH must be >= UNet batch_size.
rem   HD startup self-prewarm: the FIRST HD sentence otherwise pays a one-off GFPGAN load + grid_sample/batch
rem   autotune (~a few sec). NOW DEFAULT ON (LIPSYNC_HD_PREWARM=1): paid at boot in the GPU thread so the
rem   first HD content sentence is already full-speed. Startup also warms the STREAMING branch (merged loop +
rem   first-seg flush + trailing partial batch, via _warmup_stream) to kill first-streaming-sentence first-frame
rem   spikes. Set LIPSYNC_HD_PREWARM=0 for STD-only / small-VRAM boxes to save ~1GB VRAM (HD warms lazily then).
rem set LIPSYNC_HD_PREWARM=0
rem   LIVE-FIRST scheduler (anti GPU head-of-line-block): the single GPU worker runs each task to completion,
rem   so a running BG task (face precompute / idle-loop export, incl. LivePortrait ~7-28s) blocks a live
rem   sentence that arrives mid-way -> occasional multi-second first-frame spike (measured gpu_wait 3.5s+).
rem   Two default-ON, zero-config fixes: (1) LIPSYNC_BG_DEFER=1 holds BG GPU tasks out of the queue while a
rem   live sentence is active (last LIPSYNC_LIVE_GUARD_SEC=2.0s), capped by LIPSYNC_BG_DEFER_MAX_SEC=20 so BG
rem   never starves; (2) LIPSYNC_BG_YIELD=1 makes the precompute per-frame loop yield to a waiting live task
rem   within ~one frame. Set either to 0 to revert to the old pure-priority-queue behavior.
rem set LIPSYNC_BG_DEFER=0
rem set LIPSYNC_BG_YIELD=0
rem   A/B (2026-06, after GC fix freed RTF): shrinking STD 600/1000 -> 300/600 only cut content-sentence
rem   text->first-lip by ~60ms (noise) while adding ~67% more calls/vcam-pushes. Reason: content first-frame
rem   is gated by the SINGLE-CARD render queue (behind opener + prior sentences), not by coalescing. So we
rem   keep the bigger default blocks (fewer calls). To cut first-frame for real, add a 2nd lipsync card.
rem set CONV_TTS_STREAM_LIP_FIRST_MS=600
rem set CONV_TTS_STREAM_LIP_CHUNK_MS=1000
rem set CONV_TTS_STREAM_LIP_MAX_MS=1800
rem set CONV_TTS_STREAM_HD_FIRST_MS=1500
rem set CONV_TTS_STREAM_HD_CHUNK_MS=4000
rem set CONV_TTS_STREAM_HD_MAX_MS=5000
rem   Audience auto-answer (_audience_fire -> api_converse_stream) picks this up automatically when
rem   eligible (fish clone voice). Stack with AUDIENCE_AUTO_BREVITY for shortest stop-to-stop time.
rem   Ops: /ops capacity card shows stream vs whole-sentence TTFA; audience card shows audience p50.

rem -- Audience Q&A channel (viewers submit at /ask; host answers from console) --
rem   Off by default. Input never speaks directly; answers reuse converse(priority=0).
rem set AVATARHUB_AUDIENCE=1
rem set AUDIENCE_MAX_Q=200
rem set AUDIENCE_RATE_SEC=3
rem set AUDIENCE_MAX_LEN=200
rem   Full-queue policy: reject (default; refuse new questions when full) or evict_stale
rem   (drop the coldest+oldest pending question to admit fresh ones). Under heavy load
rem   evict_stale keeps the queue current so new hot topics aren't shut out by a frozen
rem   backlog (verified by audience_loadtest.py: fewer drops, hotter answers, lower wait).
rem set AUDIENCE_DROP_POLICY=evict_stale
rem   Unattended auto-answer worker (host can still barge in via higher priority).
rem   Runtime-togglable from console; this only sets the boot default.
rem set AUDIENCE_AUTO_ANSWER=1
rem set AUDIENCE_AUTO_POLL=3
rem set AUDIENCE_AUTO_COOLDOWN=2
rem set AUDIENCE_AUTO_LIPSYNC=1
rem   Audience-answer brevity (auto-answer path ONLY; never affects the creator's own
rem   speech). A single avatar speaks serially, so shorter answers = higher throughput
rem   and less backlog (loadtest: halving answer time ~doubled answers/min). BREVITY is
rem   appended to the persona (persona preserved); empty string disables it. MAXTOK is an
rem   optional hard token cap (0 = rely on the hint, no mid-sentence cut).
rem   (ASCII-only here: non-ASCII in a rem line desyncs cmd's parser under chcp 65001
rem    and breaks `call env_config.bat` in nested launches. Set a CJK brevity hint with
rem    real Chinese via the /setup wizard, which writes UTF-8-safe deploy.env.bat instead.)
rem set AUDIENCE_AUTO_BREVITY=Audience Q: answer in 1-2 short spoken sentences, <=40 chars.
rem set AUDIENCE_AUTO_MAXTOK=0
rem   Audience wall (OBS browser source at /wall ; ?transparent=1 for overlay). TTL =
rem   how long a manually-answered question stays on the "now answering" card (sec).
rem set WALL_NOW_TTL=45
rem   Wall realtime: dedicated WS push interval (ms) + like-popularity half-life (sec;
rem   higher-liked questions get auto-answered first, decaying so stale ones don't hog
rem   the top. 0 = pure like count, no decay).
rem set WALL_WS_INTERVAL_MS=350
rem set AUDIENCE_HOT_HALFLIFE=600
rem   Live replay/highlights (/highlights page). Each answered question + full
rem   digital-human reply is captured, persisted to highlights.db (survives restart,
rem   browse past streams by session), and renderable as a vertical image card
rem   (/api/highlights/{id}/card.png?ratio=4x5|9x16|1x1) for short-video reuse.
rem   HIGHLIGHTS_MAX = max Q&A pairs kept in the in-memory live buffer (oldest dropped;
rem   DB keeps all). FONT vars override the CJK font used on image cards.
rem set HIGHLIGHTS_MAX=100
rem set AVATARHUB_HIGHLIGHTS_DB=%~dp0highlights.db
rem set HIGHLIGHTS_FONT=C:\Windows\Fonts\msyh.ttc
rem set HIGHLIGHTS_FONT_BOLD=C:\Windows\Fonts\msyhbd.ttc

rem ==========================================
rem  COMMERCIAL DEPLOYMENT - security hardening + licensing
rem  (ASCII-only on purpose: any non-ASCII in a rem line desyncs cmd's batch parser
rem   under chcp 65001 and makes `call env_config.bat` die mid-file in nested launches,
rem   so the hub never starts. Keep this whole section ASCII. Full Chinese guide lives
rem   in the docs / the /setup wizard UI, not in this launcher-critical .bat.)
rem ==========================================
rem  Local single-machine use can leave all of these empty (zero friction). Once the Hub
rem  is exposed to LAN / phone / internet, turn the items below ON.
rem
rem  1) Admin token (guards cross-host config edits / role CRUD / voice clone / service ops /
rem     cross-machine GPU lease /api/gpu/lease/*). When set, write ops and sensitive reads from
rem     non-loopback origins must carry the token (cookie ah_token / header X-AH-Token / query
rem     ah_token). The dashboard can set it. Machine B's gpu_peer_lease.py passes it via --token.
rem     Generate one:  powershell -c "[guid]::NewGuid().ToString('N')"
rem set AVATARHUB_API_TOKEN=put-a-random-string-here
rem     Stricter: force the token even for local loopback (default 1 = trust loopback).
rem set AVATARHUB_AUTH_TRUST_LOOPBACK=0
rem     Cross-origin page allowlist (default local only). Allow other hosts/domains as needed.
rem set AVATARHUB_CORS_ORIGINS=http://192.168.1.10:9000
rem
rem  2) GPU sub-service access control (blocks any LAN box from calling 8090/7855 etc directly).
rem     Token OR source-IP allowlist; either match passes. Hub's own loopback calls never blocked.
rem     Can also use secrets\service_token.txt / service_allow_ips.txt.
rem set AVATARHUB_SERVICE_TOKEN=put-a-random-string-here
rem set AVATARHUB_SERVICE_ALLOW_IPS=192.168.1.0,192.168.1.11
rem
rem  3) Licensing / activation (turn on when selling licenses). Default 0 = evaluate only, never
rem     block (dev/demo, zero friction). When 1 and no valid license.key, premium features
rem     (HD / multi-replica) and concurrent sessions converge to trial/tier (never crashes, only
rem     degrades).
rem        Customer fingerprint:  python license_admin.py fingerprint
rem        Vendor issues a code:  python license_admin.py issue --machine <fp> --edition pro --days 365
rem        Check status:          python license_admin.py status   or  GET /api/license/status
rem set AVATARHUB_LICENSE_ENFORCE=1
rem set LICENSE_TRIAL_DAYS=14
rem
rem ==========================================
rem  Wizard-generated overrides (/setup writes deploy.env.bat). Loaded LAST so it
rem  wins over everything above. Managed by the wizard; delete the file to revert.
rem ==========================================
if exist "%~dp0deploy.env.bat" call "%~dp0deploy.env.bat"
