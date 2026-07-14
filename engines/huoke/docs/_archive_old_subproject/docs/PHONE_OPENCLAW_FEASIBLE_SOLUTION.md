# 手机端 OpenClaw 可行方案：接收任务 + 调用 AI + 用 ADB/JS 操作手机

## 目标

- **手机上有 OpenClaw**：能接收主机下发的任务，自动调用 AI，并**在手机本地**用 ADB 或 JS 操作本机（点按、输入、打开 App 等）。
- 主机只负责任务下发与可选 AI 服务；**执行与操控在手机侧完成**。

---

## 一、技术调研结论（可行路线）

### 1. 手机“自己用 ADB 操作自己”

- **Android 11+ 无线调试**：开发者选项里开启「无线调试」，手机会暴露一个端口（如 `localhost:37123`）。
- **本机连接自己**：在同一台手机上安装 **Termux**，在 Termux 里安装 `adb`（`pkg install android-tools`），用配对码执行一次 `adb pair`，然后 `adb connect 127.0.0.1:<端口>`，即可在本机用 ADB 控制本机。
- **效果**：在 Termux 里跑 Python/Shell，执行 `adb shell input tap 500 500`、`adb shell input text "hello"`、`screencap` 等，等同于“手机用 ADB 操作自己”，无需 PC 一直连着。

### 2. 用 JS 做自动化（不依赖 PC ADB）

- **Auto.js / Auto.js Pro**：基于 **Accessibility 无障碍** 的 Android 自动化，用 **JavaScript** 写脚本，可点击、滑动、找控件、输入文字、截图等，**不需要 root**，不需要 PC 上的 ADB 线。
- **特点**：脚本在手机内运行，可定时、可被其它应用或 HTTP 触发；适合“手机上的 OpenClaw 收到任务后，执行一段 JS 流程”的场景。

### 3. 其它相关技术

- **Accessibility Service**：系统级“读屏+操作”接口，任何 App 都可以申请，拿到当前界面控件树并执行点击/输入。ADB 方案或 Auto.js 底层都可用到类似能力。
- **Google Gemini / AppFunctions**：面向 AI  agent 的官方能力，目前设备与地区限制较多，可作为后续增强，不挡当前方案。

---

## 二、推荐方案总览

| 方案 | 手机端形态 | 接收任务 | 调用 AI | 操作手机 | 优点 | 缺点 |
|------|------------|----------|---------|----------|------|------|
| **A. Termux + Python + 自连 ADB** | Termux 里跑 Python | HTTP 轮询主机 API | 请求主机或云端 API | 本机执行 `adb shell` | 与现有主机 API 无缝、可复用现有 ADB 逻辑 | 需装 Termux、配对无线调试，重启后可能需重连 |
| **B. Auto.js** | Auto.js 里跑 JS 脚本 | 主机写任务文件 / 或 HTTP 拉取 | 主机或云端 API，脚本只发请求拿“下一步” | 无障碍 + JS 控件操作 | 不依赖 ADB、纯手机内运行 | 需学 Auto.js API、调试在手机上 |
| **C. 自建 Android App（Accessibility）** | 自己写的 APK | HTTP 长轮询/WebSocket | 同上 | 无障碍 API 直接调用 | 体验最统一、可上架 | 开发量最大 |

**建议优先做 A**：主机已有任务 API 和 ADB 逻辑，手机端只需在 Termux 跑一个“领任务 + 调 AI + 发 ADB 命令”的客户端，即可实现“手机有 OpenClaw，接收任务后自己调 AI 和 ADB 操作自己”。

---

## 三、方案 A 详细设计：Termux + Python + 自连 ADB

### 3.1 架构

```
主机 (PC)                            手机
  │                                   │
  │  GET /tasks?device_id=xxx         │
  │<──────────────────────────────────│  Termux 内 Python 轮询
  │  [{ task_id, type, params }]      │
  │──────────────────────────────────>│
  │                                   │  1. 解析任务
  │  POST /ai/chat (可选)             │  2. 请求 AI 得到“步骤/回复内容”
  │<──────────────────────────────────│  3. 本机 adb connect 127.0.0.1:port
  │  { "reply": "..." }               │  4. adb shell input tap/text/...
  │──────────────────────────────────>│  5. 上报结果 PUT /tasks/{id}/result
  │                                   │
```

- **任务怎么停/继续**：任务列表由主机维护；手机轮询只拉“未完成”的任务，执行完把状态/结果回传，主机标记完成。主机可下发“取消”或“暂停”标记，手机下次轮询时看到并停止该任务。

### 3.2 手机端需要装什么

1. **Termux**（F-Droid 或官网，避免 Play 版被限制）。
2. **Termux 内**：  
   `pkg update && pkg install python android-tools`  
   装好后，`adb` 在 Termux 里可用。
3. **无线调试**：设置 → 开发者选项 → 无线调试 → 开启，记下端口（如 37123）和配对码。
4. **本机 ADB 自连**（在 Termux 里执行一次，重启后通常需重做）：  
   `adb pair 127.0.0.1:<配对端口> <配对码>`  
   `adb connect 127.0.0.1:<无线调试端口>`  
   之后 `adb devices` 应能看到本机。

### 3.3 手机端 OpenClaw 客户端（Python）职责

- **轮询任务**：每隔 N 秒 `GET {host_url}/tasks?device_id={本机标识}&status=pending`，只处理未完成任务。
- **执行逻辑**：  
  - 根据 `type`（如 `telegram_send_message`）和 `params`，决定要做什么。  
  - 需要“写回复/下一步”时：请求主机或云端 AI 接口（例如 `POST /ai/chat`），把当前任务、截图或上下文发给 AI，拿到文案或步骤。  
  - 用本机 ADB 执行：先 `adb connect 127.0.0.1:<port>` 确保连着，再 `adb shell input tap ...` / `input text ...` / `screencap ...` 等（可与现有主机侧 ADB 命令对齐）。
- **上报结果**：  
  - 成功/失败：`PUT /tasks/{task_id}/result` 或 `PATCH`，body 里 `success`、`error`、可选 `screenshot_path`（若主机可访问手机共享目录可传路径，否则传 base64 或只传 success/error）。  
  - 这样主机侧“任务状态”会更新，不会一直认为任务在执行，**任务不会莫名停在那里**；手机下次轮询也不会重复领同一任务（主机只发 pending）。
- **任务停了怎么办**：  
  - 若主机支持“取消/暂停”，任务表里会有状态；手机轮询到 `cancelled` 或 `paused` 就跳过或停止执行。  
  - 若手机进程被杀：下次启动继续轮询，只拉 pending，未上报的任务会再次被拉取，可做幂等（同一 task_id 只执行一次并上报一次）。

### 3.4 主机侧需要补的接口（与“任务怎么停/继续”相关）

- **GET /tasks?device_id=xxx&status=pending**：按设备返回未完成任务（已有或扩展现有 list）。
- **PUT 或 PATCH /tasks/{task_id}/result**：手机上报执行结果，主机更新该任务状态为 completed/failed，并写入 result（success、error、screenshot 等）。  
这样“任务完成/失败”有明确终点，**不会停在中途不更新**；若需要“暂停”，可加 `PATCH /tasks/{id} body: { status: "cancelled" }`，手机端轮询到后停止执行。

---

## 四、方案 B 简述：Auto.js

- 在手机上用 **Auto.js** 写一段“主循环”脚本：  
  - 通过 HTTP 或读主机下发的任务文件（如通过 Termux 或共享目录）拿到 `{ type, params }`。  
  - 需要 AI 时用 `http.post(host_url + "/ai/chat", ...)` 拿回复或步骤。  
  - 用 Auto.js 的 **无障碍 API**（click、scroll、input 等）操作 Telegram 等 App，完成发消息等动作。  
- **任务停/继续**：由主机写“当前任务”到文件或 API，Auto.js 脚本每次循环读一次；主机把任务标为取消/暂停时，脚本读到后 break 或跳过即可。

---

## 五、方案 C 简述：自建 Android App

- 自己写一个 Android 应用，内建：  
  - **Accessibility Service**：获取界面控件、执行点击/输入。  
  - **网络客户端**：长轮询或 WebSocket 接收主机下发的任务。  
  - **AI 调用**：HTTP 请求主机或云端 API。  
- 任务生命周期由主机和 App 共同维护（状态更新、取消、暂停），逻辑同方案 A，只是跑在原生 App 里而不是 Termux。

---

## 六、总结与下一步

- **任务怎么停、怎么继续**：  
  - **继续**：手机端轮询 `GET /tasks?device_id=xxx&status=pending`，只执行 pending；执行完立刻 **PUT /tasks/{id}/result**，主机把任务标为 completed/failed，任务就“结束”不会停住。  
  - **停**：主机把任务标为 cancelled（或 paused），手机下次轮询到后不再执行该任务即可。
- **当前最可行、最容易接在你现有主机 API 上的，是方案 A（Termux + Python + 本机 ADB）**：手机上有 OpenClaw（Termux 里的 Python 客户端），接收任务、调 AI、用 ADB 操作自己；主机只发任务和收结果，任务有明确“完成/失败/取消”状态，不会停在中途不更新。

**本仓库已新增：**
- **手机端**：`openclaw_agent/`（在 Termux 中运行），实现轮询、本机 ADB 执行、结果上报；详见 `openclaw_agent/README.md`。
- **主机端**：需提供 `GET /tasks?device_id=&status=pending` 与 `PUT /tasks/{id}/result`（手机上报结果后任务状态更新，任务不会停住不更新）；可选 `PATCH /tasks/{id}` 做取消/暂停。
