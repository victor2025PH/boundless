# 真实样片投放目录 / Real sample drop-in

首页「真实案例 · 眼见为实」(`#proof`) 区块的成片画廊会自动读取本目录下的真实样片；
**文件存在即真实播放，缺失则优雅降级**为「样片按需提供 · 预约真机演示」提示（不会显示坏播放器）。

The homepage "Real Proof" (`#proof`) gallery auto-loads real samples from this folder.
When a file exists it plays for real; when missing it gracefully degrades to a
"samples on request" note (no broken players are ever shown).

## 当前文件 / Current files（2026-07-08 换用标准音色模板重制）

音色统一为项目真人音色库标准模板 **AISHELL-3 SSB0139**（「醇厚书卷」，录音棚级干净：
信噪比 161.9dB / 带宽 12.6kHz），参考音 22s。多语种克隆用 Qwen3-TTS（十语种），
韩语因当日 Qwen3 服务不可用改用 CosyVoice（同一参考音色）。全部 -16 LUFS 响度归一。

| 文件 | 内容 | 生成来源（引擎） |
|---|---|---|
| `clone-original.mp3` | 声音克隆卡「原声样本」：SSB0139 真人参考音 8.5s 精剪（压缩句间停顿） | 真人录音（非合成） |
| `clone-result.mp3` | 声音克隆卡「克隆结果」：同一音色朗读欢迎词（cos 0.80 / 自然度 0.81） | CosyVoice `/v1/tts/clone` |
| `voice-zh.mp3` | 克隆音·中文朗读（底噪 -84dB，旧版 -53dB） | Qwen3-TTS `/v1/tts/clone` |
| `voice-en.mp3` | 克隆音·英文朗读（底噪 -104dB，旧版 -43dB） | 同上，`language=en` |
| `voice-ja.mp3` | 克隆音·日文朗读（底噪 -104dB，旧版 -67dB） | 同上，`language=ja` |
| `voice-ko.mp3` | 克隆音·韩文朗读（底噪 -72dB，旧版 -32dB；「24」按全拼「이십사」以防误读） | CosyVoice（Qwen3 当日不可用） |
| `digital-human.mp4` | 活体数字人口播样片（竖版 H.264 720×1280，30s，含音轨） | 投放样片（2026-07-08 替换） |
| `digital-human-poster.png` | 视频海报帧（6s 处截帧，540×960） | ffmpeg |

## 重新生成 / Regenerate

引擎机（本机跑着 AvatarHub :9000）上执行：

```powershell
# 候选生成+客观评分（底噪/声纹 cosine/自然度/Whisper 转写校验）：
python C:\模仿音色\temp\site_voices\gen.py      # 4 模板 x 2 引擎 x 4 语种矩阵
# 参考音模板在 C:\模仿音色\voice_pack_aishell3\SSBxxxx_ref.wav(+_ref.txt)
# 之后用 ffmpeg 转 mp3（loudnorm I=-16:TP=-1.5）替换本目录同名文件，
# 并在 lib/engineContent.ts / lib/landingContent.ts / components/WaveformPlayer.tsx 更新 ?v= 版本号。
```

> 说明：所有样片必须为**已获肖像/声音授权**的素材（自有形象或授权演示位）。
> 引擎产出默认带 C2PA 可验真水印，投放前无需去水印——「可验真」本身就是卖点。
