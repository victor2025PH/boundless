# ComfyUI 出图服务（192.168.0.176 / RTX 5090）：FLUX 文生图 + PuLID 锁脸

`companion.selfie.provider`（backend=command）与 `image_autosend` 的真出图后端：
本机跑 `tools/comfy_infer.py` → HTTP 调 176:8188 的 ComfyUI（FLUX fp8 + PuLID-Flux 锁脸）。

- 端点：`http://192.168.0.176:8188`（健康检查 `GET /system_stats`；提交 `POST /prompt`；队列 `GET /queue`）
- 消费方：A 线 `SkillManager._handle_selfie_request` / B 线 `src/inbox/image_autosend.py`（均经 `tools/comfy_infer.py` 子进程）
- 模型：锁脸自拍 `flux1-dev-fp8` 20 步（PuLID 在 dev 上训练）；无脸物体图 `flux1-schnell-fp8` 4 步（可商用、更快）
- 实测（2026-07-14，16GB 空闲显存基线）：schnell 无脸 ~10s；dev+PuLID 锁脸 ~27s

## 176 上的落地物（全部在 D:\ComfyUI\）

| 文件 | 作用 |
| --- | --- |
| `main.py` 等 | ComfyUI 本体（conda env `comfyui`，`D:\Miniconda3\envs\comfyui\python.exe`） |
| `_runcomfy.bat` | 计划任务入口：`--listen 0.0.0.0 --port 8188 --lowvram`，日志覆写到 `_comfy.log` |
| `_comfy.log` | 运行日志（每次启动覆写；排障先看尾部） |
| `watchdog_comfy.ps1` | 自愈看门狗：每 5min 探 `/system_stats`，10s 无响应 → End+杀残留 → 经 ComfyBoot 重启（防抖 10min；本仓源码在 `scripts/comfy176/`） |
| `watchdog.log` | 看门狗动作日志（健康时零日志） |

计划任务：`ComfyBoot`（交互式、user 账号、每日 23:59 + 手动 /Run）拉起服务；
`ComfyWatchdog`（每 5min、/IT 交互式）跑看门狗。

## 常用命令（从 117，ssh 别名 `gpu176`，见 `~/.ssh/config`）

```powershell
ssh gpu176 "schtasks /Run /TN ComfyBoot"      # 拉起 / 重启后半段
ssh gpu176 "schtasks /End /TN ComfyBoot"      # 停止（杀任务树）
ssh gpu176 'powershell -NoProfile -Command "Get-Content D:\ComfyUI\_comfy.log -Tail 30"'   # 看日志
ssh gpu176 "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader"          # 显存水位
curl.exe -m 8 http://192.168.0.176:8188/system_stats                                        # 健康探测
# 端到端验证（本机）：
python tools/comfy_infer.py --prompt "a cup of coffee" --out tmp_selfies/_verify.png --url http://192.168.0.176:8188
```

> ssh 里 PowerShell 引号转义很容易碎：外层单引号+内层双引号最稳；复杂命令用
> `powershell -EncodedCommand <base64>`（Unicode 编码）绕开转义。176 无 wmic，用 CIM。

## 已知事故与根因（2026-07-14）

- **窗口误关杀服务**：ComfyBoot 是交互式控制台任务，有人在 176 桌面关掉窗口 → 进程随
  `forrtl: error (200) window-CLOSE event` 死亡（schtasks 上次结果 `0xC000013A`）；防火墙对无监听端口
  DROP（连接超时而非拒绝），本机只能靠「生成静默失败→用户被搪塞」发现。→ 已加 ComfyWatchdog 自愈。
- **显存互挤**：云端 LLM 超时会把本地兜底 `qwen3:30b`（16GB, keep_alive 30m）拉进同一块 5090；
  176 常驻服务动物园（facefusion/fishspeech/fitdit/ymsvc/rvc/sbv2/minicpmo）基线已占 ~16GB，
  三方叠加 → FLUX 权重流式加载饿死（`HostBuffer.read_file_slice failed` / 采样 177s/it 僵死）。
  `comfy_infer` 自带「显存不足 → 请求 ComfyUI 卸载」自愈，但对**别的进程**占的显存无能为力；
  紧急手动腾显存：
  ```powershell
  # 立即卸载 ollama 已加载模型（keep_alive 归零）
  python -c "import urllib.request,json; urllib.request.urlopen(urllib.request.Request('http://192.168.0.176:11434/api/generate', data=json.dumps({'model':'qwen3:30b-a3b-instruct-2507-q4_K_M','keep_alive':0}).encode(), headers={'Content-Type':'application/json'}), timeout=60)"
  ```
