# LAN 算力端点只读探针（2026-08-28 中枢「云端优先·算力重分配」执行单配套）
#
# 用途：一条命令回答「这台机器上到底装了什么、什么正驻留在显存里、哪些服务真的活着」。
# 全程只读（/api/tags、/api/ps、/health、/v1/models），零 GPU 占用、不加载任何模型。
#
# 为什么需要它：配置文件说要打哪个端点，与那个端点上**实际有没有那个模型**是两件事。
# 本单期间实证：中枢单子写「视觉从 176 迁 198，模型同名同版」，探针一跑就发现 198 上
# 只有 MyVisionQwen + qwen3:8b，**根本没有 qwen3-vl** —— 照单切过去就是视觉全断。
# 同理它还揭穿了「173:11434 兜底」其实零驻留（每次触发冷载 8.37GB）。
# 媒体产物验证纪律的同源精神：配置/HTTP 200 都不构成「能力到货了」的证据。
#
# 读法：
#   · /api/ps 的 expires_at 是判「谁在续命」的关键——2318 年＝永久钉（谁 pull 谁钉的），
#     具体到小时的时间戳＝某个调用方的 keep_alive 在续（据此可反推调用归属）。
#   · 4070 是 12GB、5090 是 32GB：拿驻留合计对着卡容量看，才知道还塞不塞得下新模型。
#
# 用法：powershell -ExecutionPolicy Bypass -File tools\probe_lan_endpoints.ps1

$ProgressPreference = 'SilentlyContinue'

Write-Output '########## OLLAMA /api/tags (in-stock models) ##########'
foreach ($h in @('192.168.0.198:11434', '192.168.0.176:11434', '192.168.0.140:11434', '192.168.0.173:11434')) {
    Write-Output ('=== ' + $h + ' ===')
    try {
        $r = Invoke-RestMethod -Uri ('http://' + $h + '/api/tags') -TimeoutSec 6
        if (-not $r.models) { Write-Output '  (empty model list)' }
        $r.models | Sort-Object name | ForEach-Object {
            Write-Output ('  {0,-42} {1,7:N2} GB' -f $_.name, ($_.size / 1GB))
        }
    } catch {
        Write-Output ('  UNREACHABLE: ' + $_.Exception.Message)
    }
}

Write-Output ''
Write-Output '########## OLLAMA /api/ps (currently RESIDENT in VRAM) ##########'
foreach ($h in @('192.168.0.198:11434', '192.168.0.176:11434', '192.168.0.140:11434', '192.168.0.173:11434')) {
    Write-Output ('=== ' + $h + ' ===')
    try {
        $r = Invoke-RestMethod -Uri ('http://' + $h + '/api/ps') -TimeoutSec 6
        if (-not $r.models) { Write-Output '  (nothing resident)' }
        $r.models | ForEach-Object {
            Write-Output ('  {0,-42} {1,7:N2} GB  until={2}' -f $_.name, ($_.size / 1GB), $_.expires_at)
        }
    } catch {
        Write-Output ('  UNREACHABLE: ' + $_.Exception.Message)
    }
}

Write-Output ''
Write-Output '########## NON-OLLAMA SERVICE PORTS ##########'
# 端口清单随「单点化编制」更新（2026-08-29 停电后实测对齐；老板指令「不做兜底只留一个」）：
#   智聊四条单点主路 = 104:7865 TTS / 198:8765 ASR+SER / 176:8188 出图 / 173:8001 LLM——
#   任何一行 DOWN 即该能力整链停摆（strict 不回落），排障先看这四行。
#   176:8765 已人去楼空：aitr_asr 于 08-29 迁 198（引擎 voice_recognition/speech_emotion/
#   audio_pipeline 三处配置均指 198:8765，且 198 /health 回 asr_loaded+ser_loaded）。
#   140:7852/7854 与 176:9000 是 AvatarHub 产品线资产（交接176 §4.7：modes.json 硬拦、
#   Hub SVC_STT）——智聊已不消费，探到 DOWN 属 176 线处置面，不是智聊故障。
$svc = @(
    @{n = '173:8001 vLLM chatx (LAN sole LLM)'; u = 'http://192.168.0.173:8001/v1/models' },
    @{n = '104:7865 IndexTTS-2 (chatx TTS sole)'; u = 'http://192.168.0.104:7865/health' },
    @{n = '198:8765 aitr_asr ASR+SER (sole)'; u = 'http://192.168.0.198:8765/health' },
    @{n = '176:8188 ComfyUI image-gen (sole)'; u = 'http://192.168.0.176:8188/system_stats' },
    @{n = '176:9000 AvatarHub Hub (chatx off)'; u = 'http://192.168.0.176:9000/health' },
    @{n = '176:8765 aitr_asr old home (moved 198)'; u = 'http://192.168.0.176:8765/health' },
    @{n = '176:6242 (config.yaml ref)'; u = 'http://192.168.0.176:6242/health' },
    @{n = '176:7860 realtime_voice (retired)'; u = 'http://192.168.0.176:7860/' },
    @{n = '176:8003 face_swap (retired)'; u = 'http://192.168.0.176:8003/health' },
    @{n = '140:7852 CosyVoice (AvatarHub-owned)'; u = 'http://192.168.0.140:7852/health' },
    @{n = '140:7854 Whisper STT (AvatarHub-owned)'; u = 'http://192.168.0.140:7854/health' },
    @{n = '140:8188 ComfyUI (2nd image landing?)'; u = 'http://192.168.0.140:8188/system_stats' },
    @{n = '173:7852 CosyVoice (dead cfg)'; u = 'http://192.168.0.173:7852/health' }
)
foreach ($s in $svc) {
    try {
        $resp = Invoke-WebRequest -Uri $s.u -TimeoutSec 5 -UseBasicParsing
        $body = $resp.Content
        if ($body.Length -gt 150) { $body = $body.Substring(0, 150) + '...' }
        Write-Output ('  OK   {0,-40} HTTP {1}  {2}' -f $s.n, $resp.StatusCode, ($body -replace "`r?`n", ' '))
    } catch {
        Write-Output ('  DOWN {0,-40} {1}' -f $s.n, $_.Exception.Message)
    }
}
