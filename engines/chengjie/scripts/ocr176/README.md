# PP-OCRv5 文字检测微服务（192.168.0.176:8766，CPU）

图片翻译「译文图」的专用 OCR：行级精确框替代 VLM 近似 bbox（漏块/盖偏的结构性
根因——CVPR 2026 PP-OCRv5/v6 实测通用 VLM 文字定位 Hmean 落后专用检测 ~39pp）。

- **CPU 推理是刻意选择**：176 的 5090 显存长期 ~26/32GB（qwen3:30b+vl+hy-mt+bge+
  ASR/SER），本服务 QPS 极低（坐席点一次译一张），mobile 模型 CPU 百毫秒级足够，
  且避开 Blackwell(sm_120) 的 paddle GPU 适配风险。
- 契约：`GET /health`；`POST /v1/ocr {image_b64}` →
  `{ok, lines:[{text, box:[x1,y1,x2,y2]像素, conf}], width, height, ms}`。
  消费方 `src/ai/image_patch_translate.build_ppocr_boxes_fn`（失败自动回落 VLM，
  服务掉线零断面）。
- 开关（引擎侧 overlay，热改零重启）：
  `media.image_patch_translate.ocr: {provider: ppocr, base_url: "http://192.168.0.176:8766"}`
  回退＝`provider: vlm`。bbox 缓存键分家（`bboxp:` vs `bbox:`），A/B 互不污染。

## 部署（从 117：scp 三件套 + server 到 176，然后远程跑 deploy）

```powershell
ssh gpu176 "New-Item -ItemType Directory -Force -Path C:\aitr_ocr | Out-Null"
scp scripts\ocr176\*.py scripts\ocr176\*.ps1 gpu176:C:\aitr_ocr\
ssh gpu176 "powershell -NoProfile -ExecutionPolicy Bypass -File C:\aitr_ocr\deploy_ocr.ps1"
Invoke-RestMethod http://192.168.0.176:8766/health
```

首次 predict 会从 bcebos CDN 自动下载 PP-OCRv5 模型（几十 MB，中国网络快；
176 的 HF hub 不可靠教训不适用于 paddle CDN）。开机自启=计划任务 `AITR_OCR_176`
（ONSTART/SYSTEM），自愈=`AITR_OCR_WATCHDOG`（每 5min 探 /health，8s 无响应重启；
start_ocr.ps1 的幂等判据是 /health 200——活着但 accept 死的僵尸会被收割）。

## 运维

```powershell
ssh gpu176 "schtasks /Run /TN AITR_OCR_176"          # 拉起/重启
ssh gpu176 "Get-Content C:\aitr_ocr\logs\ocr.out.log -Tail 30"
```
