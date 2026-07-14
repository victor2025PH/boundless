@echo off

rem ==========================================

rem  Secrets config TEMPLATE - copy to secrets.bat and fill in real keys:

rem      copy secrets.example.bat secrets.bat

rem  secrets.bat is gitignored and never committed.

rem  Keep this file ASCII-only (encoding-proof: safe to `call` before any chcp).

rem ==========================================

set CONV_DEEPSEEK_API_KEY=PUT_YOUR_DEEPSEEK_API_KEY_HERE



rem ---- Cloud S2S interpretation backend (optional; Volcano Engine / Seed) ----

rem  Enables INTERP_S2S_BACKEND=seed (see live_interpreter /config/s2s).

rem  New console: single API key. Old console: APP key + Access key pair.

rem  Leave blank to stay fully offline (local cascade, default).

set SEED_S2S_API_KEY=

set SEED_S2S_APP_KEY=

set SEED_S2S_ACCESS_KEY=

rem  Resource ID from console (default volc.service_type.10053 = AST 2.0):

set SEED_S2S_RESOURCE_ID=

rem  Set to seed to route direction-A (me->peer) through cloud S2S:

set INTERP_S2S_BACKEND=



rem ---- Acceptance alert channels (optional; acceptance.py pushes on failure) ----

rem  Fill any one (or more). Leave blank to disable that channel.

rem  WeCom group robot key (qyapi webhook key):

set ACCEPT_WECOM_KEY=

rem  Server-chan SendKey (push to phone):

set ACCEPT_SERVERCHAN=

rem  Generic webhook URL (receives POST JSON {subject,text,lines}):

set ACCEPT_WEBHOOK_URL=

rem  Set to 1 to alert even when acceptance passes (default: only on failure):

set ACCEPT_ALERT_ALWAYS=



rem ---- Read-only mobile dashboard share ( /share/ops?token=... ) ----

rem  Token for the read-only mobile dashboard. Leave blank to auto-generate

rem  (persisted to secrets\ops_share_token.txt; get link via GET /api/share/ops_link).

set OPS_SHARE_TOKEN=



rem ---- Management-plane auth (recommended for LAN/multi-machine) ----

rem  Set a token to require it for ALL write ops (POST/PUT/PATCH/DELETE) and

rem  sensitive reads (/api/config, /api/share/ops_link, /api/export_profiles).

rem  Carry it via cookie ah_token / header X-AH-Token / query ah_token.

rem  By default localhost (127.0.0.1) is TRUSTED so local tools/tests need no token;

rem  only NON-loopback (other LAN machines) must present it. Leave blank = disabled.

set AVATARHUB_API_TOKEN=

rem  Set to 0 to also enforce the token on localhost (full enforce; stricter).

set AVATARHUB_AUTH_TRUST_LOOPBACK=



rem ---- Biometric data at-rest encryption (recommended) ----

rem  Encrypt each profile's JSON (incl. voice_b64/face_b64 voiceprint) at rest with

rem  Fernet. In-memory stays plaintext so nothing else changes. Existing rows are

rem  auto-migrated (encrypted) on first startup after enabling.

rem  Enable by setting this to 1 (key auto-generated to secrets\profile_key.key):

set AVATARHUB_ENCRYPT_PROFILES=

rem  Optional: supply your own Fernet key (base64, 32 bytes) instead of the key file.

rem  *** BACK UP THE KEY *** losing it makes encrypted profiles unrecoverable.

set AVATARHUB_SECRET_KEY=

rem  Optional: override profiles DB path (isolated test / multi-instance):

set AVATARHUB_PROFILES_DB=

rem  NOTE: when profile encryption is ON, golden packages (golden_packages\*.zip,

rem  which embed the face + voiceprint) are ALSO encrypted at rest with the SAME key

rem  and transparently decrypted on restore - no extra config. For PORTABLE encrypted

rem  exports (move a profile to another machine) call

rem    GET /api/profile/<name>/export_package?password=<pw>   -> *.ahpkg

rem  and import with the same password (POST /api/profile/import_package, field password).



rem ---- GPU service-plane hardening (recommended for LAN/multi-machine) ----

rem  The GPU services (fish 7855 / stt 7854 / lipsync 8090 / vcam 7870 / faceswap

rem  8000 / tts 7851 ... and service_manager 9999) listen on 0.0.0.0 and were

rem  historically CORS:* with NO auth - any LAN machine could call them (steal GPU

rem  time / control service_manager). Hardening (OFF by default, fully opt-in):

rem    * Each service allows a request only if it is loopback, OR carries a valid

rem      X-AH-Svc token, OR its source IP is in the allow-list. /health + CORS

rem      preflight are always open. CORS is also narrowed from '*' to the hub origin.

rem  Pick EITHER (or both):

rem  TIP: run  harden.bat  once to auto-generate the token + print the per-machine

rem       rollout steps. RVC realtime-VC API (6242) is now covered by the same scheme.

rem  (A) Shared token - set the SAME value on every machine (hub + each service):

set AVATARHUB_SERVICE_TOKEN=

rem      (or write it to secrets\service_token.txt - same value on all machines)

rem  (B) IP allow-list - simplest for multi-machine, ZERO hub-side changes: on each

rem      service machine list the hub's LAN IP(s) (comma-separated) so it may call in:

set AVATARHUB_SERVICE_ALLOW_IPS=

rem  Narrow service CORS to extra browser origins if needed (defaults to hub origin):

rem set AVATARHUB_CORS_ORIGINS=http://192.168.0.167:9000

