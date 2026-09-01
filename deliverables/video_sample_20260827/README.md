# 智聊营销视频样片产线（实施77 渠道五打样，2026-08-27）

## 成品

| 文件 | 说明 |
|---|---|
| `chatx_sample_zh.mp4` | 中文样片 27.6s：官网中文页录屏 + 小晓神经声配音 + 烧录字幕 + bd2026.cc 水印 |
| `chatx_sample_en.mp4` | 英文样片 27.4s：官网英文页录屏 + Jenny 神经声配音 + 英文字幕 |
| `vo_zh.mp3` / `vo_en.mp3` | 配音（magic bytes + ffprobe 双验通过） |
| `footage_zh.webm` / `footage_en.webm` | 原始录屏素材（playwright 1080p） |
| `subs_zh.srt` / `subs_en.srt` | 字幕（按配音时长比例分配） |
| `frame_*.png` | 抽帧目检记录 |

## 复跑 / 扩产

```powershell
cd website; npx next start -p 3457        # 起本地站点（或改 make_sample.py 的 SITE 指生产）
cd ..\deliverables\video_sample_20260827
python make_sample.py all                  # vo → record(zh+en) → assemble
```

- **换语种**：`SEGS_*` 加语种文案 + `VOICES` 加 edge 声（印尼 id-ID-GadisNeural / 泰 th-TH-PremwadeeNeural），录屏可复用英文页画面；
- **换克隆声**：176 hub fish/index 在线（`/health` 已验），把 `vo()` 换成
  `engines/chengjie/tools/hub_tts_fetch.py` 调用即可，验证链（magic bytes + ffprobe）不变；
  公开营销素材默认用中性播音声是刻意选择（人设克隆声属聊天场景资产）；
- **纪律**：文案零业绩数字（防编造闸红线）；任何音视频产物必须过 ffprobe/magic bytes 验证才算 OK。
