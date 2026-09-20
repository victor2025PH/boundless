# 清唱模板库（song_templates）

本目录＝**出厂脚手架**。真实模板库放**实例数据根**同路径
（如 `D:\chengjie-instances\zhiliao\data\config\song_templates\`），
音频文件不入 git。

## 结构

```
config/song_templates/
  manifest.json          # 模板清单（结构见 manifest.example.json）
  pd_twinkle.wav         # 模板干声（清唱，无伴奏；15~40s 最甜）
  origin_wanan.wav
  ...
```

## 供给红线（版权，先读再入库）

只允许三类来源，`manifest.json` 的 `source` 字段如实登记：

1. **`origin` 原创买断**：委托创作/录音且**书面买断**词曲+录音权利；
2. **`pd` 公有领域**：词曲**均**过保护期（如 Twinkle Twinkle；注意「歌曲公版
   ≠ 某个录音公版」，录音必须自制或确认公版）；
3. **`origin` ACE 离线原创**：hub `/api/song/create`（ACE-Step）生成的原创曲，
   经人工筛选后取干声（`/api/song/task/{tid}/stem/vocal`）。

⛔ 绝不入库：在版权期歌曲的翻唱/扒带/原版录音分离物。产品层也不接「唱周杰伦
的晴天」类点名翻唱请求（词表刻意只认「要你唱」，不认「唱某某的歌」）。

## 干声质量要求

- 无伴奏、无和声垫、无混响拖尾（SVC 转换对干净干声效果最好）；
- 15~40 秒；起止留 0.3s+ 静音；
- 采样率 ≥ 24kHz，无削顶。

## 备货渲染

```powershell
python scripts/song_prerender.py --dry-run      # 看计划
python scripts/song_prerender.py                # 全量（夜间低峰跑）
python scripts/song_prerender.py --persona lin_xiaoyu --template pd_twinkle
```

产物落 `<数据根>/assets/voices/<persona>/songs/<template_id>.ogg` + 同名
`.json` meta（含 hub 声纹分 / 参考音指纹——换声自动触发重渲）。
首批产物必须过**人耳裁决**再开 `companion.singing.enabled`（媒体产物验证纪律：
首样验通过前禁止批量放行）。
