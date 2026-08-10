# Qwen3-TTS VoiceDesign 文字造声部署包（P2 · 2026-08-02）

## 是什么

按一句自然语言描述**凭空生成全新音色**（开源 SOTA，Qwen3-TTS-12Hz-1.7B-VoiceDesign）。
产物 wav 作为「参考音」喂给既有克隆栈（幻声 hub Fish / CosyVoice）注册成人设专属声：

```
人设卡气质描述 → /v1/voice_design 出 N 个候选样本
  → campplus 区分度过滤（与在库 12 声两两 <0.55，工具见 chengjie tools/voice_ssot_check.py 同族评分）
  → 选定样本注册 hub 档（chengjie_<pid>）+ 落 chengjie config/voice_refs/<pid>.wav
```

价值：新增人设「先造声后上岗」，不依赖真人录音、零名人克隆合规风险；
也是「声音商店 / VIP 专属声」变现线的生产工具。

## 装哪台

| 主机 | 判定 |
|---|---|
| **192.168.0.173（5090 32G）** | **推荐**。当前仅一个按需 qwen14b + 闲置 CosyVoice3，显存大量空闲。注意 173 同时是桌面试用机（deploy/desktop 有 reset 脚本在维护），装前和桌面线打个招呼 |
| 192.168.0.176（5090 32G） | 不推荐——hub 六引擎+直播+出图已经很忙 |
| 192.168.0.117（3060 12G） | **禁止**——显存已满，有 OOM 事故史 |

模型 bf16 权重 ~4-5GB 显存，服务懒加载、闲时可停。

## 部署步骤（在目标机上）

1. 拷贝本目录到目标机（如 `D:\faceX\tools\voicedesign\`）
2. `provision_qwen3_vd.bat`（幂等：conda env qwen3tts + torch cu128 + qwen-tts + modelscope 拉 1.7B-VoiceDesign 权重）
3. 自测：`...\envs\qwen3tts\python.exe qwen3_vd_server.py --selftest "温柔清亮的年轻女声，语速稍慢，带一点笑意"` → 出 `vd_selftest.wav` 试听
4. 起服务：`set QWEN3_VD_MODEL_DIR=D:\faceX\models\Qwen3-TTS-12Hz-1.7B-VoiceDesign` 后运行 `qwen3_vd_server.py`（默认 :7859）
5. 常驻化：参照 faceX `_svc_qwen3_boot.bat` 模式建计划任务（/health 判活）

## 接口

```
GET  /health → {status, engine:"qwen3_vd", model_loaded}
POST /v1/voice_design {text, instruct, language="zh"} → {ok, audio_base64(wav), sample_rate}
```

`instruct` 即音色描述，例：「三十岁上下的女声，声线温润中音，吐字清晰，像电台深夜节目主持人，语速偏慢」。
同一 instruct 每次生成音色**不保证一致**——所以产物必须固化为参考音走克隆链，而不是每次现场 VD。

## 已知事项

- 包会打印 `SoX not found` 警告：不影响本服务（仅部分音频工具链需要），可忽略或装 SoX。
- flash-attn 缺失警告：Windows 无预编译轮子，走 PyTorch 手动实现即可（faceX .117 同状态）。
- 造声样本建议 8-15 秒、含逗号停顿的自然句，作参考音效果最好。
