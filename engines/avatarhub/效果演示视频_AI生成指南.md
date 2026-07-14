# 效果演示视频 · AI 生成指南（/order 页 6 个演示位 · 4K/30 秒版）

> 页面已上线 6 个视频位（`C:\web117\lib\avatarhub-pricing.ts` 的 `SHOWCASE_VIDEOS`）。
> 「数字人口播」已用真实引擎输出标 ✓；其余 5 个为「制作中」占位。
> 成片放 `C:\web117\public\videos\showcase\<key>.mp4` → 对应条目 `ready: true` → 部署。

## 一、在哪里生成（2026-07 实况）

| 入口 | 网址 | 说明 |
| --- | --- | --- |
| **Google Flow（主力）** | https://labs.google/flow | 生成 + Ingredients 角色一致性 + Scene Builder 分镜 + 1080p/4K 放大导出 |
| Gemini App | https://gemini.google.com | 快速试验；无 4K，日限 3–5 条 |
| Gemini API / Vertex | https://ai.google.dev/gemini-api/docs/veo | 批量补量；4K 约 $0.60/秒（8 秒 ≈ $4.8） |

- **4K 必须 Google AI Ultra**（$249.99/月，新订户前 3 月 $124.99）；Pro（$19.99）最高 1080p。
- Ultra 含 25,000 积分（≈250 条 Quality），但 Quality 有每日 ~4–5 条软限 → 排 3–5 天。
- 策略：**订 1 个月 Ultra 做完即退**；草稿用 Fast 档（20 积分/条）迭代，定稿 Quality + 4K 导出。
- 需美区网络环境 + 外币卡。

## 二、4K 与 30 秒的硬约束（关键认知）

1. Veo 单次生成上限 **8 秒**；4K 只支持 8 秒单段（本质是 1080p 母版智能放大）。
2. 「延长 Extend」可拼到 148 秒，但**只支持 720p**——与 4K 互斥。
3. **正确做法：每条片拆 4 个 8 秒分镜，各自 4K 生成，剪辑拼成 ~32 秒。**
   分镜切换本来就是广告片语言，比强行一镜到底质量更高。
4. 人物一致性：先生成角色定妆照，之后每个分镜用 **Ingredients 参考图** 喂同一张脸。
5. **绝不让 AI 渲染文字**（必乱码）——字幕、品牌 LOGO、"概念演示"角标全部后期加（剪映即可）。

## 三、通用素材

**风格块（每个分镜末尾粘贴）：**

```
Cinematic tech-noir studio atmosphere, neon cyan (#22d3ee) and violet (#8b5cf6) accent lighting, photorealistic, shallow depth of field, smooth gimbal camera, high-detail skin texture, 35mm film look, no on-screen text, no captions, no watermarks.
```

**固定角色（逐字复用 + Ingredients 参考图）：**
- 主持人 A：`a confident East Asian man in his late 20s, short black hair, black hoodie`
- 切换目标 B：`a bearded European man in his 40s, slicked-back brown hair, grey blazer`
- 夜市女主 A 脸：`a young Asian woman with long black hair in a red jacket`；B 脸：`a European woman with an auburn bob`

**审核规避**：分镜涉及"同一人两张脸"若被拦，把措辞改为 `appears as a different character` / `his digital avatar`。

## 四、五条片 · 30 秒级分镜提示词（4 × 8s）

### 1. live — 直播实时换脸换声

```
Shot 1 — A confident East Asian man in his late 20s, short black hair, black hoodie, sits at a streaming desk with an RGB keyboard, adjusts his webcam, looks into camera and says calmly: "Watch this. One click." A slim holographic panel glows beside him. Audio: his natural mid-tone voice, quiet room ambience.

Shot 2 — Split-screen: left side, the same East Asian man talking energetically; right side, the SAME body, SAME motion, SAME lip-sync timing, but he appears as a bearded European man in his 40s with slicked-back brown hair. A floating latency meter pulses "42 ms" in cyan between the panels. Audio: one sentence heard twice, first young voice then deeper voice.

Shot 3 — Close-up on the desk monitor: a live waveform overlay shifts from cyan to violet mid-sentence as his voice deepens in real time. His reflection is visible on the dark screen. Audio: the same phrase sliding smoothly from a young tone to a deep tone without a cut.

Shot 4 — Camera pulls back: a smartphone propped next to the monitor shows the outgoing live stream where he appears as the bearded European man. The real man waves; the phone version waves in perfect sync. He smiles at the camera. Audio: soft triumphant synth swell.
```

### 2. faceswap — 视频换脸 · 前后对比

```
Shot 1 — Steady gimbal tracking shot: a young Asian woman with long black hair in a red jacket walks toward camera through a neon-lit night market, glancing at food stalls, natural crowd bokeh. Audio: night market ambience, distant chatter, sizzling food.

Shot 2 — The SAME continuous walking shot: a thin cyan scan-line sweeps slowly from left to right across the frame; behind the line she has a completely different face — a European woman with an auburn bob — while her motion, red jacket, lighting and the crowd remain identical. Audio: a soft electronic shimmer as the line passes.

Shot 3 — Extreme slow-motion close-up: the scan-line crosses her face; skin texture, specular highlights and neon reflections stay perfectly continuous on both sides of the line, tiny particles sparkle along the edge. Audio: low sub-bass pulse, delicate glass chimes.

Shot 4 — The two versions of her stand side by side in a frozen moment as the camera orbits thirty degrees around them, night market lights streaking into bokeh behind. Both smile simultaneously. Audio: ambient pad resolving to a warm chord.
```

### 3. voice — 声音克隆 · 情感 TTS

```
Shot 1 — Macro shot of a studio condenser microphone with cyan rim light in a dark room. A man leans into frame and says warmly: "Just thirty seconds of my voice." A cyan waveform ripples across the glass desk as he speaks. Audio: intimate dry vocal, faint room tone.

Shot 2 — The waveform lifts off the desk as a floating hologram and splits into two; the duplicate turns violet and SPEAKS ON ITS OWN in the exact same male voice, but excited and laughing. The man watches, raising an eyebrow, amused. Audio: same timbre, joyful energetic delivery.

Shot 3 — The violet hologram morphs between three glowing orbs; the same voice performs three moods in sequence: an excited announcement, a soft whisper, a calm news-anchor read. Camera slowly orbits the orbs. Audio: three emotional reads of one voice, seamless transitions.

Shot 4 — The orbs orbit a small glowing globe with soft light ribbons; the same voice speaks one sentence in English, then flows into Japanese mid-breath with identical timbre. The man nods approvingly in the background bokeh. Audio: bilingual line, one voice, gentle outro pad.
```

### 4. interp — 克隆音实时同传

```
Shot 1 — Split-screen video call: left, a professional Chinese businesswoman in a bright modern office speaks Mandarin to her laptop with natural hand gestures; right, an American male client in a loft office listens attentively. Audio: her clear Mandarin sentence, office ambience.

Shot 2 — A glowing translation node appears between the two panels: her cyan Mandarin waveform flows into it and exits violet on the client's side — he hears fluent English IN THE SAME FEMALE VOICE, her lips subtly re-synced. A small meter reads "1.2 s". Audio: the same female voice now in English.

Shot 3 — The client replies in English; the flow reverses through the node and the businesswoman hears natural Mandarin IN HIS VOICE. Both nod and smile, unaware of any language gap. Audio: his English then the same male timbre speaking Mandarin.

Shot 4 — Wide symmetrical shot of both panels: they laugh at the same joke simultaneously, reach toward their screens in a mirrored gesture like a handshake across the split. Violet and cyan light ribbons connect the panels. Audio: shared laughter, warm resolving chord.
```

### 5. studio — 换发型 · 定妆 · 试衣

```
Shot 1 — A young woman stands relaxed in a futuristic fitting room facing a floor-to-ceiling smart mirror; soft icon thumbnails glow along the mirror edge. She wears a plain white shirt, long black hair. Audio: quiet high-end boutique ambience, subtle UI blips.

Shot 2 — A horizontal band of cyan light sweeps down from head to toe: her look transforms into a silver bob with a black leather jacket — SAME pose, SAME smile, fabric settling naturally. She checks her reflection, pleased. Audio: airy whoosh synced to the sweep.

Shot 3 — Two more sweeps in sequence as she turns forty-five degrees between looks: wavy red hair with an emerald evening dress, then a ponytail with casual streetwear. Each transformation is instant and photoreal. Audio: two musical whooshes rising in pitch.

Shot 4 — Camera orbits her as the smart mirror shows all four looks as living reflections side by side; she touches the glass to pick the evening dress, a soft sparkle confirms. She smiles at camera. Audio: elegant final chord with a glass ting.
```

### 6. avatar — 数字人口播（已有真实输出 ✓，可选做 AI 片头）

```
Macro shot: a single portrait photo lying on a glass desk begins to ripple like water; the person in the photo blinks, lifts their head out of the frame in 2.5D parallax, and starts speaking to camera with perfect lip-sync while a teleprompter-style script scrolls faintly in the reflection. Neon cyan data particles flow from the photo edges. Audio: a confident voice saying "One photo. One script. Your presenter never sleeps."
```

## 四点五、每日自动发布（2026-07-11 起）

> 官网 `/videos` + Telegram 频道 + YouTube 三端每日自动发一条，只需把成片丢进
> `D:\projects\模仿音色\publish\queue\`（计划任务 `AvatarHubPublish` 每天 10:30 取最早一条）。
> 文件名带关键词（live/faceswap/voice/interp/studio/avatar）自动套文案，
> 或配同名 `.json` 自定义（字段见 `publish\publish_daily.py` 开头注释）。
> `/order` 页 6 个固定展示位仍按下面第五节手动上架（那是常驻栏目，不走每日流）。

## 四点六、真实视频换脸出片（faceswap_batch，2026-07-12 起）

> 不用 AI 生成、直接在真实视频上换脸，两窗天生逐帧同步（AI 做不到）。所有命令用
> facefusion 环境的 python：`C:\Users\user\Miniconda3\envs\facefusion\python.exe`。

**底层单片：`faceswap_video.py`**（跑一条）

```
python faceswap_video.py --input 素材.mp4 --output 成片.mp4 \
  --main-face faces\美女.jpg --corner-face faces\男人.jpg --corner tr \
  --delogo 6:1055:212:150      # 去水印矩形 x:y:w:h，多个用分号隔开；不填=不去
```

- `--main-face` 大画面换成的脸（必填）；`--corner-face` 右上小窗换的脸（不填=只换大窗）。
- `--delogo` 静态水印坐标：先用「多帧叠加」找位置——
  `ffmpeg -i 素材.mp4 -vf "tmix=frames=128" -frames:v 1 avg.jpg`，静态水印会凸显出来。
- RTX 5090 开 GFPGAN 增强约 1 帧/0.4s（18s 片≈3–4 分钟）；加 `--no-enhance` 更快。

**批量出片：`faceswap_batch.py`**（读 `publish\faceswap_jobs.json`，一次多条，可直接进队列）

- `compose` 模式：喂**任意普通单人口播视频**，自动排成竖版 720×1288 + 右上角真人小窗
  （同段画面缩小），只换大窗脸为美女、小窗保留真人 → 两窗完美同步。**素材随处可得，推荐量产用这个。**
- `swap` 模式：素材本身就是双窗片（如 stodownload），直接大窗换美女 + 角落换男人 + 去水印。
- 每条 job 填 `title_zh/en`、`desc_zh/en`，`queue:true` 出片后自动进 `publish\queue\`，
  次日走三端自动发布。命令：`python faceswap_batch.py`（`--only 名字` 只跑某条，`--no-queue` 只出片不入队）。

**换成你自己的脸**：把人脸图（正脸、清晰、无遮挡）放进 `faces\`，在 job 里把 `main_face`/
`corner_face` 指过去即可。一张图就够，不用训练。

## 五、生成 → 上架流程（/order 展示位）

1. Flow 里每个 Shot 用 Fast 出 2–3 版草稿 → 选构图 → Quality 重出 → 导出选 4K。
2. 剪映拼 4 段（硬切或 0.2s 叠化）→ 加中文字幕 + 片尾品牌卡（BOUNDLESS · usdt2026.cc）+「概念演示」角标。
3. 保留 4K 母版（YouTube / 给客户）；网页版压 1080p：
   `ffmpeg -i master4k.mp4 -c:v libx264 -crf 23 -preset slow -vf scale=1920:-2 -c:a aac -b:a 128k -movflags +faststart <key>.mp4`
   （30 秒控制在 8–15MB；/order 播放器 preload="none"，不拖慢页面。）
4. 放 `C:\web117\public\videos\showcase\<key>.mp4`（英文配音版命名 `<key>-en.mp4` 并在 `SHOWCASE_VIDEOS` 补 `srcEn`）。
5. `ready: true`（AI 片不标 `real` → 自动显示「概念演示」徽章；日后真实录屏替换同名文件并标 `real: true` → 变 ✓ 真实引擎输出）。
6. 部署：老流程 tar + scp + deploy.sh（或直接丢给我，我来压缩上架部署）。

## 六、费用速算（本批任务）

- Ultra 1 个月（首三月促销价 $124.99）：20 条 4K 成片 + 40 条 Fast 草稿 ≈ 4,800 积分，额度富余，做完即退订。
- 纯 API 备选：20 × 8s × $0.60（4K）= $96 + 草稿 ≈ $48 → 总 ~$144，无 UI 无 Ingredients，不推荐首选。
- 后续批量出营销短视频再考虑 Grok（$30/月）或 Veo API 混用：Grok 出量、Veo 出精品。
