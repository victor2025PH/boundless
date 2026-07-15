# 主控机 API 调用文档
> 主控机 IP：192.168.0.117  
> 服务端 IP：192.168.0.166（RTX 4070 12GB）  
> 更新日期：2026-05-29

---

## 一、服务总览

| 服务 | 端口 | 说明 | 状态 |
|------|------|------|------|
| **AvatarHub** | `9000` | 统一控制中心，所有功能入口 | ✅ 核心 |
| **FaceSwap API** | `8000` | 换脸，base64 图像输入/输出 | ✅ 运行 |
| **TTS API (XTTS-v2)** | `7851` | 基础文字转语音 + 声音克隆 | ✅ 运行 |
| **LipSync API (MuseTalk)** | `8090` | 音频驱动口型同步视频生成 | ✅ 运行 |
| **EmotionTTS (CosyVoice3)** | `7852` | 情感语音合成，支持 happy/sad/angry 等 | ✅ 运行 |
| **SingingTTS (GPT-SoVITS v4)** | `7853` | 高质量唱歌/TTS，零样本音色克隆 | ✅ 新增 |
| RVC API | `6242` | 实时变声 | 按需启动 |

> **推荐使用 AvatarHub (9000) 作为唯一入口**，它自动整合所有子服务。

---

## 二、换脸 API（端口 8000）

### 健康检查
```bash
curl http://192.168.0.166:8000/health
```

### POST /faceswap — 图片换脸

**请求：**
```json
POST http://192.168.0.166:8000/faceswap
Content-Type: application/json

{
  "source_image": "<base64编码的人脸图片，换成谁的脸>",
  "target_image": "<base64编码的目标图片，被换脸的图>"
}
```

**响应：**
```json
{
  "result_image": "<base64编码的换脸结果图片>",
  "elapsed_ms": 1230
}
```

**Python 调用示例：**
```python
import requests, base64

def faceswap(source_image_path: str, target_image_path: str) -> bytes:
    with open(source_image_path, "rb") as f:
        source_b64 = base64.b64encode(f.read()).decode()
    with open(target_image_path, "rb") as f:
        target_b64 = base64.b64encode(f.read()).decode()

    resp = requests.post(
        "http://192.168.0.166:8000/faceswap",
        json={"source_image": source_b64, "target_image": target_b64},
        timeout=120
    )
    resp.raise_for_status()
    result_b64 = resp.json()["result_image"]
    return base64.b64decode(result_b64)

# 使用
result_bytes = faceswap("my_face.jpg", "target_photo.jpg")
with open("output.png", "wb") as f:
    f.write(result_bytes)
print("换脸完成 → output.png")
```

---

## 三、TTS 语音克隆 API（端口 7851）

### 健康检查
```bash
curl http://192.168.0.166:7851/health
```

### 1. OpenAI 兼容接口 — 用预设音色合成

**请求（与 OpenAI TTS API 完全兼容）：**
```bash
curl -X POST http://192.168.0.166:7851/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "xtts_v2",
    "input": "你好，我是AI语音助手。",
    "voice": "female_01",
    "language": "zh-cn"
  }' \
  --output speech.wav
```

**可用 voice 列表：**
```bash
curl http://192.168.0.166:7851/
# 返回 JSON 中 "voices" 字段列出所有可用音色
```

**Python 调用（OpenAI SDK 直接对接）：**
```python
from openai import OpenAI

client = OpenAI(
    api_key="not-needed",          # 本地服务不验证 key
    base_url="http://192.168.0.166:7851/v1"
)

response = client.audio.speech.create(
    model="xtts_v2",
    voice="female_01",
    input="你好，我是AI语音助手。",
    extra_body={"language": "zh-cn"}
)
response.stream_to_file("output.wav")
```

---

### 2. 声音克隆接口 — 上传参考音频克隆任意音色

**方式 A：base64 传参**
```python
import requests, base64

def clone_voice(text: str, reference_wav_path: str, language: str = "zh-cn") -> bytes:
    with open(reference_wav_path, "rb") as f:
        ref_b64 = base64.b64encode(f.read()).decode()

    resp = requests.post(
        "http://192.168.0.166:7851/v1/audio/clone",
        json={
            "text": text,
            "language": language,
            "reference_audio_base64": ref_b64
        },
        timeout=60
    )
    resp.raise_for_status()
    audio_b64 = resp.json()["audio_base64"]
    return base64.b64decode(audio_b64)

# 使用：用 target_voice.wav 的音色说出指定文字
audio = clone_voice("请帮我完成这项任务", "target_voice.wav")
with open("cloned.wav", "wb") as f:
    f.write(audio)
```

**方式 B：直接上传 WAV 文件（返回 WAV 流）**
```python
import requests

with open("reference.wav", "rb") as f:
    resp = requests.post(
        "http://192.168.0.166:7851/v1/audio/clone/upload",
        data={"text": "这是克隆的声音", "language": "zh-cn"},
        files={"file": ("ref.wav", f, "audio/wav")},
        timeout=60
    )

with open("result.wav", "wb") as f:
    f.write(resp.content)
```

---

## 四、参考音频文件目录

服务端参考音频存放路径：
```
C:\模仿音色\alltalk_tts\voices\
```

**查看可用音色：**
```bash
curl http://192.168.0.166:7851/
```

**添加自定义音色：**
把 10-30 秒的干净 WAV 文件放到服务端 `C:\模仿音色\alltalk_tts\voices\` 目录，
重启 TTS API 后即可通过 `"voice": "文件名(不含.wav)"` 调用。

---

## 五、在 Windsurf 中集成（完整示例）

```python
# ai_services.py — 主控机调用封装
import requests
import base64
from pathlib import Path

SERVER = "http://192.168.0.166"

class AIServices:
    def faceswap(self, source_path: str, target_path: str, output_path: str):
        """换脸：source 是目标人脸，target 是被换脸的图"""
        with open(source_path, "rb") as f:
            s = base64.b64encode(f.read()).decode()
        with open(target_path, "rb") as f:
            t = base64.b64encode(f.read()).decode()
        r = requests.post(f"{SERVER}:8000/faceswap",
                         json={"source_image": s, "target_image": t}, timeout=120)
        r.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(r.json()["result_image"]))
        return output_path

    def tts(self, text: str, voice: str = "female_01",
            language: str = "zh-cn", output_path: str = "out.wav"):
        """文字转语音"""
        r = requests.post(f"{SERVER}:7851/v1/audio/speech",
                         json={"model": "xtts_v2", "input": text,
                               "voice": voice, "language": language},
                         timeout=60)
        r.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(r.content)
        return output_path

    def clone_tts(self, text: str, ref_wav: str,
                  language: str = "zh-cn", output_path: str = "cloned.wav"):
        """声音克隆：用 ref_wav 的音色说出 text"""
        with open(ref_wav, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        r = requests.post(f"{SERVER}:7851/v1/audio/clone",
                         json={"text": text, "language": language,
                               "reference_audio_base64": b64},
                         timeout=60)
        r.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(r.json()["audio_base64"]))
        return output_path


# 使用示例
if __name__ == "__main__":
    ai = AIServices()

    # 换脸
    ai.faceswap("source_face.jpg", "photo.jpg", "result.png")

    # TTS
    ai.tts("你好世界", voice="female_01", output_path="hello.wav")

    # 声音克隆
    ai.clone_tts("请帮我完成任务", ref_wav="my_voice.wav", output_path="cloned.wav")
```

---

## 六、服务启动命令（服务端执行）

```bat
rem 一键启动所有服务（推荐）
C:\模仿音色\start_all_services.bat

rem 或单独启动
C:\模仿音色\start_faceswap_api.bat   rem 换脸 8000
C:\模仿音色\start_tts_api.bat         rem TTS 7851
C:\模仿音色\start_avatar_hub.bat      rem AvatarHub 9000
```

---

## 七、快速连通测试（主控机执行）

```powershell
# 测试所有服务健康
$svcs = @{AvatarHub=9000; FaceSwap=8000; TTS=7851; LipSync=8090; EmotionTTS=7852}
$svcs.GetEnumerator() | ForEach-Object {
    try { Invoke-RestMethod "http://192.168.0.166:$($_.Value)/health" -TimeoutSec 3
          Write-Host "OK: $($_.Key) :$($_.Value)" } catch { Write-Host "FAIL: $($_.Key)" }
}
```

---

## 八、AvatarHub 统一 API（端口 9000）★ 推荐

### 控制面板
```
http://192.168.0.166:9000/ui
```

### 健康检查（聚合所有子服务状态）
```bash
curl http://192.168.0.166:9000/health
```

响应示例：
```json
{
  "status": "ok",
  "active_profile": "小女孩",
  "services": {"faceswap": true, "tts": true, "lipsync": true, "emotion_tts": true},
  "latency_ms": {"faceswap": 12, "tts": 8, "lipsync": 15},
  "pressure": "green",
  "gpu_util": 45
}
```

---

### POST /avatar/speak — 声脸联动（核心接口）

**功能**：文字 → TTS/情感TTS → （可选）口型同步视频，一键返回

**请求：**
```json
POST http://192.168.0.166:9000/avatar/speak
Content-Type: application/json

{
  "text": "大家好，今天天气真不错！",
  "profile": "小女孩",
  "language": "zh-cn",
  "emotion": "happy",
  "instruct": "",
  "generate_lipsync": false,
  "include_face": false,
  "incognito": false
}
```

**参数说明：**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `text` | str | 必填 | 要说的文字（最大 5000 字符）|
| `profile` | str | `""` | 角色名，空则用当前激活角色 |
| `language` | str | `"zh-cn"` | 语言：zh-cn/en/ja/ko |
| `emotion` | str | `"neutral"` | 情感：见下表；**`"auto"`** = 自动从文本检测情感（Phase 4）|
| `instruct` | str | `""` | 自然语言情感描述，如"用激动的语气" |
| `generate_lipsync` | bool | `false` | 是否生成口型同步 MP4 视频 |
| `include_face` | bool | `false` | 是否抓取实时画面并换脸 |
| `incognito` | bool | `false` | 隐身模式，不写历史记录 |

**emotion 可选值：**

| emotion | 效果 |
|---------|------|
| `neutral` | 平静（走基础 TTS，最快）|
| **`auto`** | **自动从文本内容检测情感**（Phase 4，推荐）|
| `happy` | 开心愉快 |
| `sad` | 悲伤难过 |
| `angry` | 愤怒 |
| `fearful` | 恐惧害怕 |
| `surprised` | 惊讶 |
| `disgusted` | 厌恶 |
| `gentle` | 温柔轻柔 |
| `excited` | 兴奋激动 |
| `calm` | 平静沉着 |
| `serious` | 严肃认真 |

**响应：**
```json
{
  "audio_base64": "<WAV音频 base64>",
  "face_image": "",
  "elapsed_ms": 4500,
  "rvc_applied": false,
  "warning": "",
  "lipsync_video_b64": "<MP4视频 base64，仅 generate_lipsync=true 时有值>",
  "detected_emotion": "excited"  // emotion='auto' 时返回实际检测结果
}
```

**Python 调用示例：**
```python
import requests, base64

SERVER = "http://192.168.0.166:9000"

def avatar_speak(text, emotion="neutral", profile="", lipsync=False):
    r = requests.post(f"{SERVER}/avatar/speak", json={
        "text": text,
        "emotion": emotion,
        "profile": profile,
        "generate_lipsync": lipsync,
        "language": "zh-cn"
    }, timeout=120)
    r.raise_for_status()
    data = r.json()

    # 保存音频
    audio_bytes = base64.b64decode(data["audio_base64"])
    with open("output.wav", "wb") as f:
        f.write(audio_bytes)

    # 保存口型视频（若请求了）
    if data.get("lipsync_video_b64"):
        video_bytes = base64.b64decode(data["lipsync_video_b64"])
        with open("lipsync.mp4", "wb") as f:
            f.write(video_bytes)
        print(f"视频已保存: lipsync.mp4")

    return data

# 基础调用（无情感）
avatar_speak("你好，欢迎使用数字人系统！")

# 情感调用（手动指定）
avatar_speak("今天真的太开心了！", emotion="happy")

# Phase 4：自动情感检测 ★ 推荐
r = requests.post(f"{SERVER}/avatar/speak", json={
    "text": "气死我了！！你太过分了！！",
    "emotion": "auto"  # 自动检测为 angry
}, timeout=60)
print(r.json()["detected_emotion"])  # "angry"

# 自然语言情感描述
avatar_speak("请注意安全", instruct="用严肃认真带有警告意味的语气说")

# 生成口型同步视频
avatar_speak("口型同步演示！", emotion="excited", lipsync=True)
```

---

### GET /profiles — 角色列表
```bash
curl http://192.168.0.166:9000/profiles
```

### POST /profiles — 创建角色
```json
POST http://192.168.0.166:9000/profiles
Content-Type: application/json

{
  "name": "主播小美",
  "face_b64": "<人脸图 base64>",
  "voice_b64": "<参考音频 base64，用于声音克隆>",
  "voice_name": "",
  "rvc_model": "",
  "rvc_strict_mode": false
}
```

### POST /profiles/{name}/activate — 激活角色
```bash
curl -X POST http://192.168.0.166:9000/profiles/主播小美/activate
```

---

## 九、情感TTS API（端口 7852）

> 需要 EmotionTTS 服务运行（CosyVoice3 0.5B，约3GB VRAM）

### 获取支持的情感列表
```bash
curl http://192.168.0.166:7852/v1/emotions
```

### POST /v1/tts — 基础情感合成
```python
import requests, base64

r = requests.post("http://192.168.0.166:7852/v1/tts", json={
    "text": "今天真的太开心了！",
    "emotion": "happy",
    "speed": 1.0,
    "return_base64": True
}, timeout=60)

audio_b64 = r.json()["audio_base64"]
with open("happy.wav", "wb") as f:
    f.write(base64.b64decode(audio_b64))
```

### POST /v1/tts/instruct — 自然语言情感描述
```python
r = requests.post("http://192.168.0.166:7852/v1/tts/instruct", json={
    "text": "请注意！前方危险！",
    "instruct": "用非常紧张急促的语气，语速要快",
    "return_base64": True
}, timeout=60)
```

### POST /v1/tts/clone — 克隆音色 + 情感
```python
with open("my_voice.wav", "rb") as f:
    ref_b64 = base64.b64encode(f.read()).decode()

r = requests.post("http://192.168.0.166:7852/v1/tts/clone", json={
    "text": "我来给你讲个悲伤的故事",
    "reference_audio_b64": ref_b64,
    "reference_text": "这是参考音频的文字内容",  # 提供则更准确
    "emotion": "sad",
    "return_base64": True
}, timeout=60)
```

---

## 十、口型同步 API（端口 8090）

> MuseTalk 1.5，需要 ~4GB VRAM，处理时间约 20-30s/10s音频

### POST /lipsync/precompute_face — 预计算人脸（加速后续生成）
```python
import requests

with open("face.jpg", "rb") as f:
    r = requests.post("http://192.168.0.166:8090/lipsync/precompute_face",
        files={"face": ("face.jpg", f, "image/jpeg")},
        data={"face_id": "anchor_face"},  # 自定义 ID，后续复用
        timeout=60)
print(r.json())  # {"ok": true, "face_id": "anchor_face", "latents": 1}
```

### POST /lipsync/generate — 生成口型视频
```python
import requests

with open("audio.wav", "rb") as af:
    r = requests.post("http://192.168.0.166:8090/lipsync/generate",
        files={"audio": ("audio.wav", af, "audio/wav")},
        data={
            "face_id": "anchor_face",  # 用预计算缓存（若未预计算则同时传 face 图）
            "fps": 25,
            "batch_size": 8
        },
        timeout=120)

with open("lipsync.mp4", "wb") as f:
    f.write(r.content)
```

**返回**：MP4 视频二进制流（`Content-Type: video/mp4`）

---

## 十一、唱歌 API（端口 7853）★ Phase 3 新增

> GPT-SoVITS v4，高质量零样本音色克隆，支持唱歌模式

### 通过 AvatarHub 调用（推荐）

```python
import requests, base64

SERVER = "http://192.168.0.166:9000"

def avatar_sing(lyrics: str, speed: float = 0.85, ref_wav_path: str = "") -> bytes:
    payload = {
        "lyrics": lyrics,
        "language": "zh",
        "speed": speed
    }
    if ref_wav_path:
        with open(ref_wav_path, "rb") as f:
            payload["reference_audio_b64"] = base64.b64encode(f.read()).decode()

    r = requests.post(f"{SERVER}/avatar/sing", json=payload, timeout=120)
    r.raise_for_status()
    return base64.b64decode(r.json()["audio_base64"])

# 用默认音色演唱
audio = avatar_sing("长亭外，古道边，芳草碧连天。")
with open("sing_output.wav", "wb") as f:
    f.write(audio)

# 用自定义参考音色演唱
audio = avatar_sing("让我们荡起双桨", ref_wav_path="my_voice.wav")
```

**响应字段：**
```json
{
  "audio_base64": "<WAV base64>",
  "elapsed_ms": 11400,
  "warning": ""  // 非空表示已降级至 CosyVoice3
}
```

> 若 singing_server(7853) 不可用，自动降级为 EmotionTTS gentle 模式。

### 直接调用 singing_server（高级）

```python
import requests, base64

SERVER = "http://192.168.0.166:7853"

# 基础 TTS（高质量零样本克隆）
r = requests.post(f"{SERVER}/v1/tts", json={
    "text": "你好，这是高质量语音合成测试。",
    "text_lang": "zh",
    "reference_audio_b64": "<参考音频 base64>",
    "reference_text": "参考音频里说的话",
    "reference_lang": "zh",
    "speed": 1.0,
    "return_base64": True
}, timeout=120)

audio = base64.b64decode(r.json()["audio_base64"])

# 唱歌模式（慢速逐字）
r = requests.post(f"{SERVER}/v1/tts/sing", json={
    "lyrics": "让我们荡起双桨，小船儿推开波浪",
    "text_lang": "zh",
    "speed": 0.75,
    "return_base64": True
}, timeout=120)
```

---

## 十二、智能情感检测 API（Phase 4 新增）

自动分析文本情感，无需额外模型，毫秒级响应。

### GET /api/emotion_detect — 检测文本情感

```python
import requests

SERVER = "http://192.168.0.166:9000"

# 简单检测
r = requests.get(f"{SERVER}/api/emotion_detect",
                 params={"text": "气死我了！！你太过分了！！"})
print(r.json())  # {"emotion": "angry", "text": "气死我了..."}

# 详细得分
r = requests.get(f"{SERVER}/api/emotion_detect",
                 params={"text": "太棒了！！！我好激动！！", "detail": "true"})
print(r.json())
# {
#   "emotion": "excited",
#   "scores": {"excited": 9, "happy": 2, ...},
#   "confidence": 0.75,
#   "total_signals": 12
# }
```

**支持的情感（11种）：** `neutral / happy / sad / angry / fearful / surprised / disgusted / gentle / excited / calm / serious`

### 自动情感说话（推荐用法）

```python
# emotion='auto'：系统自动检测文本情感并选择对应 TTS 模式
r = requests.post(f"{SERVER}/avatar/speak", json={
    "text": "我好伤心，眼泪都流下来了",
    "emotion": "auto"  # 自动检测为 sad
})
data = r.json()
print(f"检测情感: {data['detected_emotion']}")  # sad
```

---

## 十三、快速集成示例（完整流程）

```python
"""
完整数字人管线：文字 → 情感语音 → 口型同步视频
"""
import requests, base64, time

SERVER = "http://192.168.0.166"

def digital_human_speak(text: str, emotion: str = "happy", lipsync: bool = True):
    t0 = time.time()
    r = requests.post(f"{SERVER}:9000/avatar/speak", json={
        "text": text,
        "emotion": emotion,
        "generate_lipsync": lipsync,
        "language": "zh-cn"
    }, timeout=180)
    r.raise_for_status()
    data = r.json()
    elapsed = time.time() - t0
    print(f"耗时: {elapsed:.1f}s | 情感: {emotion}")

    # 音频
    audio = base64.b64decode(data["audio_base64"])
    with open("output.wav", "wb") as f: f.write(audio)

    # 口型视频
    if data.get("lipsync_video_b64"):
        video = base64.b64decode(data["lipsync_video_b64"])
        with open("output.mp4", "wb") as f: f.write(video)
        print(f"视频: output.mp4 ({len(video)//1024}KB)")

    return data

# 示例
digital_human_speak("大家好！今天我来给大家介绍一款新产品！", emotion="excited", lipsync=True)
digital_human_speak("非常感谢大家的支持，我们会继续努力。", emotion="gentle", lipsync=False)

# Phase 4：让系统自动判断情感
digital_human_speak("气死我了！！你太过分了！！", emotion="auto", lipsync=False)
# detected_emotion 会返回 "angry"
```

---

## 十四、服务健康检查（快速验证）

```powershell
# 主控机 PowerShell 一键检测所有服务
$ip = "192.168.0.166"
$svcs = @{
    AvatarHub  = 9000
    FaceSwap   = 8000
    TTS        = 7851
    LipSync    = 8090
    EmotionTTS = 7852
    SingingTTS = 7853
}
$svcs.GetEnumerator() | Sort-Object Value | ForEach-Object {
    try {
        $r = Invoke-RestMethod "http://${ip}:$($_.Value)/health" -TimeoutSec 3
        Write-Host "[OK  ] $($_.Key) :$($_.Value)" -ForegroundColor Green
    } catch {
        Write-Host "[FAIL] $($_.Key) :$($_.Value)" -ForegroundColor Red
    }
}
```

```python
# Python 版健康检查
import httpx

SERVER = "192.168.0.166"
services = {
    "AvatarHub":  9000,
    "FaceSwap":   8000,
    "TTS":        7851,
    "LipSync":    8090,
    "EmotionTTS": 7852,
    "SingingTTS": 7853,
}

for name, port in services.items():
    try:
        r = httpx.get(f"http://{SERVER}:{port}/health", timeout=3)
        print(f"[OK  ] {name}:{port}")
    except Exception as e:
        print(f"[FAIL] {name}:{port} - {e}")
```
