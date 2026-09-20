# 人脸嵌入边车（192.168.0.176 / CPU onnx / 端口 8767）——#333 视觉身份层

客户发来的图里**是谁**：人设本人（我方相册图被回传）/ 客户本人 / 客户确认过的关系人 / 未知。
本目录只是「算向量」的一段；判定与措辞在客户端 `src/companion/visual_identity.py`，
记忆在 `src/companion/visual_memory.py`（`<config_dir>/visual_memory.db`），接线在
`src/companion/face_identity.py` → `persona_reply` extra_hint 单一消费口。

- 端点：`GET /health`、`POST /v1/face/embed {image_base64, max_faces, min_det_score}` →
  `faces[{bbox, det_score, embedding[512], gender, age}]`（大脸优先，L2 归一，余弦=点积）、
  `POST /v1/face/compare {a_base64, b_base64}` → `cosine`。
- 模型：复用 PuLID 已装的 InsightFace **antelopev2**（`D:\ComfyUI\models\insightface\models\antelopev2`：
  scrfd_10g 检测 + glintr100 识别），**零新下载、零显存**（CPUExecutionProvider 钉死）。
- 实测（2026-09-17 部署当晚）：服务端 100～130 ms/张、117→176 往返 150～260 ms；同图增广
  （翻转/JPEG60/裁 80%/缩 480）余弦 0.935～0.971；同人设 PuLID 相册两两 0.51～0.65；
  **不同人设生成脸两两最高 0.455**（生成「大众美颜脸」互相偏像）→ 客户端判同人阈 0.50、
  0.35～0.50 疑似（`visual_identity.MATCH_THRESHOLD / AMBIGUOUS_THRESHOLD`，provisional）。
- 授权：InsightFace 权重**非商用**（代码 MIT）。PoC / 内网自用可以；**进出货包前**换 Apache-2.0
  的 AuraFace——**已备好**：`D:\ComfyUI\models\insightface\models\auraface\`（fal/AuraFace-v1，含 LICENSE，
  428MB，2026-09-17 从 117 下载 scp 过去），切换只改 `start_face.ps1` 的 `AITR_FACE_MODEL=auraface`
  再 `schtasks /Run /TN AITR_FACE_176`。
  **同机同图 A/B（2026-09-17，11 张人设 face_ref + 5 张同人设相册）**：antelopev2 impostor top 0.455 /
  同人中位 0.520 / 翻转自一致 0.937；AuraFace impostor top **0.533** / 同人中位 0.468 / 翻转 0.873，
  单张 55ms 持平。AuraFace 分离度在生成脸上差一档——**内网继续用 antelopev2**；切 AuraFace 前先用
  `tools/face_identity_calibrate.py` 在真实入站样本上重定阈（预计 MATCH 要抬到 ~0.55）。
  OpenCV YuNet+SFace（37MB）是坐席机本地兜底候选，精度再低一档，未接。
- 隐私：服务无状态不落图；客户端只存 512 维向量，`VisualMemoryStore.delete_conv` 整会话删。

## 176 上的落地物（全部在 C:\aitr_face\）

| 文件 | 作用 |
| --- | --- |
| `face_server.py` | FastAPI 本体（懒加载 + 预热，推理锁；bbox 大脸优先） |
| `start_face.ps1` | 计划任务入口：/health 幂等闸、收割僵尸监听、日志轮转、env |
| `deploy_face.ps1` | 幂等部署：uv venv(py3.12) + insightface/onnxruntime/cv2-headless/fastapi + 防火墙 8767 + 计划任务 + 起服务 |
| `watchdog_face.ps1` | 每 5min 探 /health，8s 无响应经 start_face.ps1 重启（健康零日志） |

计划任务：`AITR_FACE_176`（ONSTART, SYSTEM, HIGHEST）+ `AITR_FACE_WATCHDOG`（每 5min）。
日志：`C:\aitr_face\logs\face.out.log` / `face.err.log` / `watchdog.log`。

## 常用命令（从 117，ssh 别名 gpu176）

```powershell
# 部署 / 升级（改了 face_server.py 之后）
scp scripts\face176\face_server.py gpu176:C:/aitr_face/face_server.py
ssh gpu176 "powershell -NoProfile -ExecutionPolicy Bypass -File C:\aitr_face\deploy_face.ps1"
# 只重启
ssh gpu176 "powershell -NoProfile -Command Get-NetTCPConnection -LocalPort 8767 -State Listen | %% { Stop-Process -Id `$_.OwningProcess -Force }; schtasks /Run /TN AITR_FACE_176"
Invoke-RestMethod http://192.168.0.176:8767/health
```

## 客户端开关

`vision.face_identity.{enabled, base_url, timeout_sec, min_det_score, persona_proto_max, confirm_window_sec}`
（`config.example.yaml` 有全注释）。zhiliao overlay 与 `config.desktop.internal.yaml` 已开（LAN 直连）；
`config.desktop.min.yaml` 客户包**未开**——外网坐席机够不着 176，需网关加 `/api/face/*` 路由后再指
`bd2026.cc`。人设原型缓存 `<config_dir>/face_prototypes/persona_<pid>.json`（来源图 mtime 变即重算）。

## 门禁

```powershell
python -m pytest tests\test_visual_identity.py tests\test_face_identity.py -q
```
