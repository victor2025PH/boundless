# demo_record · /order 页效果演示「真实录屏」流水线

> 与《效果演示视频_AI生成指南.md》(AI 概念片)互补:这里出的是**真实引擎输出**,
> 上架后标 `real: true` → 页面显示「✓ 真实引擎输出」徽章,比概念片更有说服力。

## 一句话用法

```powershell
# 1. 录制(全屏接管约 1.5–4 分钟,期间别动鼠标键盘;Hub 需在 9000 端口运行)
& C:\Users\user\Miniconda3\envs\facefusion\python.exe demo_record\driver.py --scene voice

# 2. 后期(去水印 → 精剪 → 字幕 → 品牌尾卡)
& C:\Users\user\Miniconda3\envs\facefusion\python.exe demo_record\postprod.py `
    --in demo_record\out\voice_take3_raw.mp4 --out demo_record\out\voice_demo.mp4 `
    --keep "3.4-19.5,29.8-46.5" --caption "0.5,5,全程真实界面实录" --endcard
```

## 组成

| 文件 | 作用 |
| --- | --- |
| `recorder.py` | Bandicam 命令行控制(`/record` `/stop`),等文件落盘 |
| `driver.py` | Playwright kiosk 全屏打开 Hub,渲染假光标逐步操作;`--scene` 选场景 |
| `postprod.py` | ffmpeg 后期:去 Bandicam 水印(delogo) + 选段拼接 + 字幕 + 尾卡 + 1080p |
| `restore_bandicam.ps1` | 恢复录制前的 Bandicam 设置(原值在 `bandicam_backup.json`) |
| `raw/` | Bandicam 原始录像落盘处(已在注册表指过来) |
| `out/` | 成片、节拍 JSON(`*_beats.json`,记录每步发生在第几秒,剪辑参考) |

## 已改的 Bandicam 设置(注册表 HKCU\Software\BANDISOFT\BANDICAM\OPTION)

- `nRunAsAdmin 2→0`:**关键修复**。原来 Bandicam 自提权,脚本拉起时 UAC 无人确认→进程直接死。
- 全屏录制主屏(1920×1080)、输出目录 `demo_record\raw`、60fps、16Mbps、不录真实光标。
- **未注册版**有顶部居中水印(`www.BANDICAM.com`)和单段 10 分钟上限:
  水印已实测坐标 `x=776 y=2 w=344 h=38`,postprod 的 delogo 默认干净去除;
  若日后输入注册码,把 postprod 加 `--no-delogo` 即可。

## 场景/成片清单

| /order 位 | 产出脚本 | 状态 |
| --- | --- | --- |
| 声音克隆 · 情感 TTS | `driver.py --scene voice` → postprod | ✅ 已上架(voice.mp4) |
| 克隆音实时同传 | `driver.py --scene interp` → postprod | ✅ 已上架(interp.mp4,真实链路) |
| 视频换脸 · 前后对比 | `compose_faceswap_demo.py`(扫描线+双窗真人小窗+四宫格) | ✅ 已上架(faceswap.mp4) |
| 换发型 · 定妆 · 试衣 | `gen_studio_stills.py` + `compose_studio_demo.py` | ✅ 已上架(studio.mp4,含合成 BGM) |
| 数字人口播 | — | ✅ 早已上线(avatar) |
| 直播实时换脸换声 | 待录:真人正脸坐 BRIO 前 → 开播 real_faceswap 双窗录屏 | ⏳ 等真人出镜 |

**同传要点**:录前 `interp_session_up()` 起会话(麦=CABLE Output 收注入的中文,克隆音出扬声器)。
必须先 `voicelock/reset`——旧会话遗留的声纹锁会把注入的克隆音判为"非注册说话人"全程拦截。

**换脸/换装素材**:明星脸图在 `C:\Users\user\Desktop\明星`;`faceswap_video.py --main-face/--corner-face`
换任意脸;`gen_studio_stills.py` 换 `刘亦菲/林志玲` 及发型/妆容/服装名即可重出。

## 上架(已接入 /order)

`publish_showcase.py` 一步:web 优化(1080p/crf23/faststart)+ 生成 poster + 拷到
`C:\web117\public\videos\showcase\<key>.mp4`。`avatarhub-pricing.ts` 的 `SHOWCASE_VIDEOS`
对应 4 条已置 `ready:true, real:true` + poster。改完走原 deploy 流程部署。

## 录制注意

- 录前把 Hub 顶部的运维告警横幅点掉(或等 driver 后续版本自动清横幅),画面更干净。
- 录制中 TTS 播放声走系统默认输出,Bandicam WASAPI 直接收进音轨(实测 max -0.1dB)。
- 每段场景失败会自动截图 `out/<tag>_error.png` 并保留原片,便于对时间轴排查。

## 上架(同 AI 指南第五节)

1080p 成片放 `C:\web117\public\videos\showcase\<key>.mp4` → `avatarhub-pricing.ts`
对应条目 `ready: true, real: true` → 部署。30 秒控制在 8–15MB(当前 crf21 约 3MB/分钟,达标)。
