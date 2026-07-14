# AvatarHub · 实时数字人系统

> 语音克隆 + 活体口型 + 换脸 + WebRTC 直播的本地实时数字人平台。
> 核心链路：**说话 → STT → LLM → 克隆音 TTS → 流式口型(活体) → WebRTC / OBS 虚拟摄像头**。

---

## 1. 快速开始

**方式 A — 桌面启动器（推荐）**：双击 `launcher.bat`，在图形界面里一键「启动核心链路 / 启动全部 / 停止 / 重启选中」，并实时显示每个服务的就绪状态（● 就绪 / 加载中 / 停止）、打开控制台、一键体检。

> 启动器分两层：品牌化界面 `launcher_qt.py`（PySide6，运行在隔离的 `.venv_launcher`）；若无 PySide6 则自动回退到零依赖的 `launcher.py`（tkinter，随 Python 自带）。
> 首次启用 PySide6 界面（一次性）：
> ```bat
> <facefusion环境的python> -m venv .venv_launcher
> .venv_launcher\Scripts\python -m pip install pyside6-essentials
> ```

**方式 B — 命令行**：

```bat
:: 1) 一键启动核心链路（实时对话最小集），就绪后自动放行（ready.py 实时探针，不再盲等）
start_all_services.bat

:: 2) 需要换脸 / 唱歌 / 高清口型 / 情感 TTS 等扩展服务：
set START_EXTRAS=1
start_all_services.bat
```

其它运维命令：

```bat
python service_manager.py status     :: 查看各服务真实存活/健康（含外部拉起的）
python service_manager.py daemon     :: 守护模式：自动重启崩溃的核心服务 + 管理 API :9999
python ready.py                      :: 仅探测核心链路是否就绪（退出码 0=全就绪）
python ready.py --all                :: 探测全部服务
```

启动后访问统一控制台：**http://127.0.0.1:9000/ui**
手机端对话：**http://<本机局域网IP>:9000/phone**

> 路径与 conda 环境**全自动探测**（见 §4），换机/换用户名/换盘符无需改代码。

---

## 2. 系统架构

中央编排器 `avatar_hub.py`（端口 9000）聚合下列微服务，每个服务跑在独立 conda 环境中以隔离依赖：

| 服务 | 脚本 | 端口 | conda 环境 | 核心链路 | 说明 |
|------|------|------|-----------|:------:|------|
| AvatarHub 中枢 | `avatar_hub.py` | 9000 | facefusion | ✅ | 角色管理 / 编排 / API / 控制台 |
| 克隆音 TTS | `fish_speech_server.py` | 7855 | fishspeech | ✅ | Fish-Speech S2 零样本克隆 |
| 克隆音 TTS(可商用) | `voxcpm_server.py` | 7856 | voxcpm | ⬜ | VoxCPM2（Apache-2.0·48kHz·Voice Design） |
| 克隆音 TTS(低延迟) | `qwen3_tts_server.py` | 7858 | qwen3tts | ⬜ | Qwen3-TTS（双轨流式·首包~97ms·3秒克隆·10语种） |
| 语音转文字 | `stt_server.py` | 7854 | cosytts | ✅ | Whisper STT |
| 语音转文字(流式) | `nemotron_stt_server.py` | 7857 | nemoasr | ⬜ | Nemotron 3.5（流式·可商用·WS 增量） |
| 口型同步 | `lipsync_server.py` | 8090 | musethepeak | ✅ | MuseTalk + LivePortrait 活体 |
| 广播中枢 | `vcam_server.py` | 7870 | facefusion | ✅ | OBS 虚拟摄像头 + WebRTC |
| 换脸 | `faceswap_api.py` | 8000 | facefusion | ⬜ | 可插拔换脸(inswapper/256高清·FACESWAP_MODEL) + GFPGAN/CodeFormer + 光流时序平滑 + TensorRT FP16(FACESWAP_TRT) |
| 情感 TTS | `emotion_tts_server.py` | 7852 | cosytts | ⬜ | CosyVoice 情感语音 |
| 高清口型 | `latentsync_server.py` | 8091 | latentsync | ⬜ | LatentSync 512 离线扩散 |
| 人脸增强/超分 | `enhance_server.py` | 8092 | facefusion | ⬜ | GFPGAN 人脸复原 + Real-ESRGAN 整帧超分(离线HD) |
| 唱歌 | `singing_server.py` | 7853 | musethepeak | ⬜ | GPT-SoVITS |
| 发型 | `hair_api.py` | 8001 | facefusion | ⬜ | HairFastGAN |
| 变声(离线) | RVC `api_240604.py` | 6242 | rvc | ⬜ | Retrieval-based VC |
| Coqui TTS | `tts_api.py` | 7851 | rvc | ⬜ | XTTS（兜底） |

> LLM 默认接本机 **Ollama**（如 `qwen2.5:14b`，端口 11434），可经 env 覆盖。
> 服务清单的**单一真相**在 `app_config.py` 的 `SERVICES`，供中枢 / 自检 / 启动器共用。

### 关键支撑模块
- `engine_registry.py`：TTS/VC/LipSync/FaceSwap 引擎注册表（可插拔适配器）。
- `conversation.py`：对话编排（STT→LLM→TTS→口型 流式双流、barge-in、安全闸门、混合 RAG：BM25 词法 + 语义嵌入 RRF 融合 + MMR 去冗余精排 + 查询嵌入缓存·缺嵌入后端自动降级 BM25·答案带引用脚注）+ 跨会话长期记忆（按 user 持久化关于用户的事实、按需召回注入、后台合并整合摘要+记忆）+ 共情自适应（读懂用户情绪→引导回应口吻+语音情感联动·多轮情绪轨迹：连续负面升级关怀/由负转正给鼓励）；记忆带时效衰减（按类型分半衰期：身份永驻、偏好衰快）+ 冲突消解（改喜好新覆盖旧）。情绪检测覆盖烦躁/焦虑/心累等口语负面。
- `provenance.py`：合规溯源（C2PA 嵌入 + Ed25519 签名 + 不可见水印）。
- `metrics.py`：对话可观测指标。
- `live_base.py` / `face_enhance.py` / `gfpgan_clean.py`：活体基底与人脸增强。

---

## 3. 目录结构

```
.
├─ avatar_hub.py            中央编排器（9000）
├─ app_config.py            ★统一配置层（路径/conda/服务清单自动解析）
├─ launcher.py / launcher.bat  ★桌面启动器（tkinter GUI，零新依赖）
├─ service_manager.py       服务编排器（start/stop/restart/status/daemon，清单派生自 app_config）
├─ ready.py                 就绪探针（纯标准库，并发探测 /health）
├─ doctor.py                一键体检
├─ *_server.py / *_api.py   各微服务入口
├─ conversation.py engine_registry.py provenance.py metrics.py ...  核心库
├─ static/                  前端页面（ui / phone / converse / landing / dashboard）
├─ requirements/            各 conda 环境依赖基线（pip freeze 快照）
├─ logs/                    运行日志
├─ archive/                 已归档的一次性脚本与历史备份（不参与运行）
│   ├─ oneoff/              历史调试 / 验证 / 一次性迁移脚本
│   └─ web_backup/          旧版页面备份
├─ env_config.bat           启动环境变量（自动探测 BASE_DIR / CONDA_ROOT / 各 env python）
├─ start_all_services.bat   一键启动
└─ 升级开发路线图_v3.md      开发路线图与完整开发日志
```

---

## 4. 配置与可移植性

所有路径**零硬编码**，由 `app_config.py` 解析，优先级：**环境变量 > `config.json` > 自动探测**。

- **项目根** `BASE`：由 `app_config.py` 自身位置自动推导（也可 `set AVATARHUB_BASE=...` 覆盖）。
- **conda 根** `CONDA_ROOT`：从当前解释器 / `CONDA_PREFIX` / 常见安装位置多策略探测（也可 `set AVATARHUB_CONDA_ROOT=...`）。
- **各环境 python**：`<CONDA_ROOT>\envs\<env>\python.exe`，可按环境名用 `set AVATARHUB_PY_<ENV>=...` 覆盖。
- **可选覆盖文件**：复制 `config.example.json` 为 `config.json`，仅在跨机差异时填写（如模型放别的盘、非标准 conda 根）。

自检配置解析是否正确：

```bat
<某环境>\python.exe app_config.py
```

多机分工（把语音/换脸放到另一台机器）：见 `env_config.bat` 顶部的 `SVC_*` 说明。

机密（云端 API Key）放在 `secrets.bat`（已被 `.gitignore` 忽略），模板见 `secrets.example.bat`。

### 4.1 商用部署：安全加固与授权

> 本机自用可全部留空（零摩擦）。一旦把 Hub 暴露到局域网 / 手机 / 外网，按下表加固。详细开关见 `env_config.bat` 的「COMMERCIAL DEPLOYMENT」段。

- **管理面令牌** `AVATARHUB_API_TOKEN`：设后，非回环来源的写操作与敏感读须带令牌（cookie `ah_token` / 头 `X-AH-Token`）。挡住同网段任意机器改配置 / CRUD 角色 / 克隆声音。
- **GPU 子服务令牌** `AVATARHUB_SERVICE_TOKEN` / `AVATARHUB_SERVICE_ALLOW_IPS`：保护 8090/7855 等算力口，仅 hub / 白名单可调。
- **离线授权激活**（对外卖授权时）：厂商持私钥签发、产品仅公钥离线验签，按机器指纹绑定 + 有效期 + 档位（trial / standard / pro）控能力。

```bat
:: 客户机：取本机指纹（发给厂商）
python license_admin.py fingerprint
:: 厂商：用私钥签发授权码 license.key
python license_admin.py issue --machine <指纹> --edition pro --days 365 --licensee "客户名"
:: 查看状态（任一）
python license_admin.py status
:: 或浏览器 / 运维：GET http://127.0.0.1:9000/api/license/status
```

> 开启强制：`set AVATARHUB_LICENSE_ENFORCE=1`（默认 0 = 只评估不拦截）。强制下若无有效授权，HD / 多副本 / 并发会话按试用档收敛——**只降级、绝不崩**。厂商私钥 `secrets\license_vendor_sk.pem` 务必保密、勿入库（`.gitignore` 已忽略）。

---

## 5. 依赖与环境

每个 conda 环境的依赖基线快照在 `requirements/<env>.txt`（`pip freeze` 生成）。

**一键体检 / 准备环境**（推荐，新机部署用）：

```bat
provision.bat              :: 体检：列出 6 个 conda 环境与关键模型目录是否就位（只读）
provision.bat --create     :: 对缺失环境按 requirements\ 基线自动创建并装依赖（幂等）
```

启动器里也有「环境体检」按钮，等价于 `provision.bat`。

手动重建某个环境（示例）：

```bat
conda create -n facefusion python=3.10 -y
conda activate facefusion
pip install -r requirements\facefusion.txt
```

> 注意：含 PyTorch CUDA（cu128 等）与本机/源码安装包，跨机重建需按显卡（如 RTX 5090 = sm_120 需 CUDA 12.8 / torch≥2.7）调整。详见路线图中的环境恢复记录。

---

## 6. 健康自检

```bat
<facefusion>\python.exe doctor.py
```

启动脚本会在服务拉起后自动运行一次自检，全绿即可使用。

---

## 7. 打包为可执行程序

把图形启动器打成单文件 exe（`dist\AvatarHub.exe`，带品牌图标 + 版本信息）：

```bat
:: 一次性准备（若尚未安装）：
.venv_launcher\Scripts\python -m pip install pyside6-essentials pyinstaller pillow
:: 生成图标（改图标后重跑）：
.venv_launcher\Scripts\python assets\make_icon.py
:: 打包：
build_launcher.bat
```

产物 `dist\AvatarHub.exe` 需与项目代码放在同一目录（冻结态下自动以 exe 所在目录为项目根）。
自检：`set AVATARHUB_SELFTEST=1` 后运行 exe，会轮询一次并把结果写入 `selftest_result.txt` 后退出。

> 说明：exe 只是「前门」控制台；各 AI 微服务仍运行在各自 conda 环境（见 §5）。

### 7.1 生成 Windows 安装包（Inno Setup）

```bat
:: 需先有 dist\AvatarHub.exe（上一步），并安装 Inno Setup 6（https://jrsoftware.org/isdl.php）
installer\build_installer.bat
```

产物 `dist\AvatarHub-Setup-1.0.0.exe`（约 36 MB）。特点：

- **每用户安装**到可写目录（免管理员，程序可在自身目录旁写 logs/config）；
- 自动创建开始菜单 / 桌面快捷方式，含卸载程序；
- 安装前展示前置说明，安装后若未检测到 conda 会给出 provision 指引；
- **只打包**控制台 exe + 编排脚本 + 前端 + 依赖基线 + 文档；**绝不打包** conda 环境、模型、机密（`secrets.bat` / `llm_backends.json` 已显式排除）。

> 安装脚本：`installer\AvatarHub.iss`（UTF-8 BOM）。模型与 conda 环境仍按 §5 在目标机单独 provision。
