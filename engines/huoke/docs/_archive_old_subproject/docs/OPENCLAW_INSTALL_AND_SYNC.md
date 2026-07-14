# OpenClaw 安装流程与主机/手机协同方案

## 一、整体关系（谁装什么、谁和谁通信）

```
┌─────────────────────────────────────────────────────────────────┐
│  主机 (PC / 服务器)                                              │
│  • 安装：本项目 (mobile-auto-project) + 运行 server.py            │
│  • 职责：下发任务、接收结果、查看设备在线、可选提供 AI             │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 局域网 HTTP (WiFi 同网)
                            │ 手机轮询 GET /tasks、上报 PUT /tasks/{id}/result
                            │ 可选：POST /heartbeat、GET /devices
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  每台手机                                                         │
│  • 安装：Termux + openclaw_agent（本项目里的 openclaw_agent 目录）│
│  • 职责：轮询任务、本机 ADB 操作自己、调 AI（可选）、上报结果     │
└─────────────────────────────────────────────────────────────────┘
```

- **主机**：只装一份，可给多台手机发任务；手机通过 **device_id** 区分，主机按 device_id 分配任务。
- **手机**：每台装 Termux + openclaw_agent，配置自己的 **device_id** 和主机的 **host_url**，即可与主机协同。

---

## 二、安装流程

### 2.1 主机侧安装（先做）

| 步骤 | 操作 |
|------|------|
| 1 | 电脑已安装 Python 3.8+、ADB（可选，用于 USB 调试时发现设备） |
| 2 | 进入项目目录：`cd mobile-auto-project` |
| 3 | 安装依赖：`pip install -r requirements.txt`（若缺 FastAPI/uvicorn：`pip install fastapi uvicorn pyyaml requests`） |
| 4 | 配置设备（可选）：编辑 `config/devices.yaml`，可先留空或填已知设备；主机 API 也支持“手机首次上报时自动登记” |
| 5 | 启动服务：`python server.py`，默认监听 `0.0.0.0:8000` |
| 6 | 确认本机 IP（手机要填）：Windows `ipconfig`，Linux/Mac `ifconfig` 或 `ip a`，记下局域网 IP（如 `192.168.1.100`） |
| 7 | 防火墙放行 8000 端口（局域网访问） |

**验证**：浏览器打开 `http://<本机IP>:8000/docs` 能看到 API 文档；或 `curl http://127.0.0.1:8000/devices` 有 JSON 返回。

### 2.2 手机侧安装（每台手机做一遍）

| 步骤 | 操作 |
|------|------|
| 1 | Android 11+，开启 **开发者选项** → **无线调试**，记下「配对码、配对端口、无线调试端口」 |
| 2 | 安装 **Termux**（建议 [F-Droid 版](https://f-droid.org/en/packages/com.termux/)），打开 Termux |
| 3 | 安装基础包与 ADB：`pkg update && pkg install python android-tools` |
| 4 | **本机 ADB 自连**（在 Termux 执行，重启后需重做）：<br>• `adb pair 127.0.0.1:<配对端口> <配对码>`<br>• `adb connect 127.0.0.1:<无线调试端口>`<br>• `adb devices` 能看到 `127.0.0.1:xxxxx device` |
| 5 | 获取本机 **device_id**（与主机识别该手机）：<br>• `adb -s 127.0.0.1:<无线调试端口> shell getprop ro.serialno`<br>若无输出可用：`adb -s 127.0.0.1:<端口> get-serialno`<br>记下输出（如 `89NZVGKFD6BYUO5P`），即本机 device_id |
| 6 | 把 **openclaw_agent** 放到手机：<br>• 方式 A：电脑 `adb push mobile-auto-project/openclaw_agent /sdcard/Download/openclaw_agent`，手机在 Termux 里 `cp -r /sdcard/Download/openclaw_agent ~/openclaw_agent`<br>• 方式 B：手机 Termux 里 `git clone <项目地址>` 后只保留 `openclaw_agent`<br>• 方式 C：U 盘/网盘拷贝整个 `openclaw_agent` 到手机后放进 Termux 可访问目录 |
| 7 | Termux 里进入目录：`cd ~/openclaw_agent`（或你放置的路径） |
| 8 | 安装 Python 依赖：`pip install -r requirements.txt` |
| 9 | 配置：`cp config.example.yaml config.yaml`，编辑 `config.yaml`：<br>• `host_url`: `http://<主机局域网IP>:8000`<br>• `device_id`: 第 5 步得到的序列号（也可留空，由程序在 ADB 连接后自动读取）<br>• `adb_port`: 无线调试端口（与第 4 步一致） |
| 10 | 试运行：`python agent.py`，看日志是否“拉取任务”“本机 ADB 已连接”；Ctrl+C 退出 |
| 11 | 常驻运行（任选）：<br>• `nohup python agent.py > agent.log 2>&1 &`<br>• 或配合 Termux:Boot 等实现开机自启 |

**验证**：主机上创建一条测试任务（见下），手机应在一次轮询周期内领到并执行（或上报结果）。

---

## 三、主机与手机如何沟通、协同工作

### 3.1 通信方式（统一用 HTTP，手机主动拉）

- **手机 → 主机**  
  - **拉任务**：`GET /tasks?device_id=<本机 device_id>&status=pending`  
    主机返回该设备下所有「待执行」任务列表。  
  - **上报结果**：`PUT /tasks/<task_id>/result`，body：`{ "success": true/false, "error": "错误信息", "screenshot_path": "可选" }`  
    主机把该任务标为已完成/失败，并保存结果，任务不会一直停在“执行中”。  
  - **心跳（可选）**：`POST /heartbeat` 或每次 GET /tasks 视为心跳，主机据此判断设备“最近在线”。

- **主机 → 手机**  
  - 不主动推；主机只通过「返回 GET /tasks 的响应体」把任务下发给手机。  
  - 若以后要做“取消/暂停”，主机把任务状态改为 cancelled，手机下次拉到的列表里不再包含该任务即可。

这样：**所有“指令”都是手机轮询拉取的，主机只提供 API 和存储**，无需手机公网 IP、无需长连接。

### 3.2 协同流程（一次任务的完整生命周期）

1. **主机**：人或其他系统调用 `POST /tasks` 创建任务，body 里指定 `device_id`（或留空表示“任意一台在线设备”）、`type`、`params`。  
2. **主机**：把任务写入存储，状态为 `pending`。  
3. **手机**：定时（如每 10 秒）请求 `GET /tasks?device_id=xxx&status=pending`。  
4. **主机**：返回该 device_id 下所有 pending 任务。  
5. **手机**：对每个任务按 type 执行（如本机 ADB 打开 Telegram、发消息）；需要 AI 时再请求主机或云端 AI 接口。  
6. **手机**：执行完后请求 `PUT /tasks/<task_id>/result` 上报 success/error。  
7. **主机**：更新该任务为 completed/failed，并保存 result。  
8. 下次手机轮询时，该任务已非 pending，不再被领走；**任务结束，不会停住不更新**。

### 3.3 主机需要提供的接口（与手机协同的最小集合）

| 接口 | 用途 | 手机/主机谁用 |
|------|------|----------------|
| `GET /tasks?device_id=&status=pending` | 手机拉取待执行任务 | 手机轮询 |
| `PUT /tasks/<task_id>/result` | 手机上报执行结果 | 手机执行完调用 |
| `POST /tasks` | 创建任务（指定 device_id/type/params） | 主机侧人工或脚本 |
| `GET /tasks`（或带 status 筛选） | 查询任务列表/状态 | 主机/调试 |
| 可选：`GET /devices`、`POST /heartbeat` | 设备列表、在线状态 | 主机看“哪些手机在线” |

只要主机实现上述「拉任务 + 上报结果」和「创建任务」，手机上的 OpenClaw 就能与主机协同工作；心跳/设备列表是增强，可后补。

### 3.4 多台手机如何协同

- 每台手机在 `config.yaml` 里配置**不同的 device_id**（建议用 `adb get-serialno` 或 `ro.serialno`，保证唯一）。  
- 主机创建任务时指定 **device_id**：则该任务只会被该设备拉走；不指定则可由主机逻辑分配“任意一台”或“第一台在线设备”。  
- 主机可通过「最近一次收到该 device_id 的请求时间」判断设备在线，便于界面显示“哪些手机已安装 OpenClaw 且在线”。

---

## 四、安装后自检清单

**主机**

- [ ] `python server.py` 能启动，无报错  
- [ ] 浏览器访问 `http://<本机IP>:8000/docs` 正常  
- [ ] 能 `POST /tasks` 创建一条测试任务（device_id 填某台手机的 serial），且 `GET /tasks` 能看到该任务  

**手机**

- [ ] Termux 里 `adb devices` 能看到 `127.0.0.1:端口`  
- [ ] `config.yaml` 里 host_url、device_id、adb_port 正确  
- [ ] `python agent.py` 能跑，日志里有“拉取任务”或“本机 ADB 已连接”  
- [ ] 主机创建一条该 device_id 的 pending 任务后，手机能在一次轮询内领到并执行或上报结果  

---

## 五、小结

- **安装顺序**：先装主机并启动 server，再在每台手机上装 Termux + openclaw_agent，配置 host_url 与 device_id，即可开始协同。  
- **沟通方式**：手机定时拉任务（GET /tasks）、执行后上报结果（PUT /tasks/…/result）；主机只提供 HTTP API，不主动推。  
- **协同关系**：主机负责任务存储与下发、可选 AI；手机负责领任务、本机 ADB 操作、上报结果；通过 **device_id** 与 **status** 区分设备与任务状态，任务有明确“完成/失败”终点，不会停住不更新。

按此方案安装并配置后，主机与多台手机即可稳定协同工作。
