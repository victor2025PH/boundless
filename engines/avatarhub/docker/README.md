# AvatarHub · Linux GPU 推理面容器化（云部署脚手架）

> 对应《云服务与远程代部署方案.md》第三节「云 GPU 工作池」：把四个**纯 HTTP GPU 微服务**
> 跑在任意 Linux + NVIDIA 机器上（AutoDL / RunPod / 阿里云 / 客户机房），Windows 中枢
> （Hub/直播/通话面）不动，通过 `SVC_*` 指向远端。Windows 桌面交付**零影响**。

## 架构对齐

| Windows 侧 | 容器侧 | 说明 |
| --- | --- | --- |
| conda env（fishspeech 等） | 镜像内 venv | 依赖同源：`docker/requirements/*.linux.txt` 由 `gen_linux_requirements.py` 从 `requirements/*.txt` 自动生成 |
| 项目根目录 | 卷挂载 `/app` | 同一套代码与模型目录形态，`AVATARHUB_BASE=/app` |
| `AVATARHUB_SERVICE_TOKEN` | 同名环境变量 | GPU 服务面鉴权一致（service_auth.py） |
| `provision.py --create` | `docker compose build` | 环境准备的两条腿 |

镜像**只含依赖**，代码与模型运行时挂载——升级代码/模型不重建镜像，与 Windows 侧
「装好环境后直接跑项目目录」的心智一致。

## 服务清单

| compose 服务 | 脚本 | 端口 | 用途（云化优先级依方案文档） |
| --- | --- | --- | --- |
| `fish_tts` | fish_speech_server.py | 7855 | 克隆音 TTS（云变声/云翻译共用） |
| `stt` | stt_server.py | 7854 | Whisper 语音转文字（云翻译） |
| `lipsync` | lipsync_server.py | 8090 | MuseTalk 口型（云数字人） |
| `faceswap` | faceswap_api.py | 8000 | 换脸（离线云换脸最先变现） |

## 快速开始（Linux GPU 机器）

```bash
# 0) 前置：NVIDIA 驱动 + nvidia-container-toolkit（nvidia-smi 可用）
# 1) 同步项目根到本机（代码 + 模型目录，rsync/网盘均可）
# 2) 生成/更新 Linux 依赖清单（Windows 侧或 Linux 侧执行均可，产物入库）
python docker/gen_linux_requirements.py

# 3) 构建（首次约 15-30 分钟，此后依赖不变零重建）
docker compose -f docker/docker-compose.yml build

# 4) 启动（全部，或只起需要的子集）
AVATARHUB_SERVICE_TOKEN=你的令牌 docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml up -d fish_tts stt   # 例：只做云变声+云翻译

# 5) 验收
docker compose -f docker/docker-compose.yml ps        # 等 healthy
curl http://127.0.0.1:7855/health
```

## Windows 中枢接入远端 GPU 面

`secrets.bat`（或 `config.json` 的服务地址覆盖）里加：

```bat
set SVC_FISH_TTS=http://<linux机IP>:7855
set SVC_STT=http://<linux机IP>:7854
set SVC_LIPSYNC=http://<linux机IP>:8090
set SVC_FACESWAP=http://<linux机IP>:8000
set AVATARHUB_SERVICE_TOKEN=与容器侧相同的令牌
```

启动器/doctor 的健康检查自动跟随 `SVC_*`，无需其他改动。

## 依赖清单再生成

Windows 基线 `requirements/<env>.txt` 是单一真相；改动后重跑：

```bash
python docker/gen_linux_requirements.py            # 全部
python docker/gen_linux_requirements.py --only fishspeech
python docker/gen_linux_requirements.py --selftest # 转换规则离线自测
```

转换规则：剔 Windows-only 包（pywin32/pyvirtualcam/sounddevice/PyAudio…，留痕注释）、
`-e c:\...` 可编辑安装移入 `*.editable.txt`（容器启动时对挂载卷内源码树秒装）、
torch 家族按 `+cuXXX` 自动补 PyTorch 轮子源（nightly 钉版转 `--pre` 最新，注释留原钉版）。

## 已知边界

- **模型不进镜像**：项目根的 `fish-speech/`、`MuseTalk/`、`models/`、`checkpoints/` 等
  需随代码同步到 Linux 机器（与 Windows 部署一致，README 模型准备章节通用）。
- **nightly torch**（facefusion 环境）：Windows 基线钉的 dev 版轮子会滚动下架，Linux 清单
  转为「同 CUDA 家族最新 nightly」。如需严格复现，改清单钉回可用版本。
- **实时链路延迟**：换脸/口型走云端需 RTT < 80ms 才适合直播（方案文档第二节）；
  离线任务（云换脸出片、TTS 合成）不受限。
- **Windows 专属面不容器化**：VB-Cable 通话、OBS 虚拟摄像头、直播采集仍在 Windows 侧。
