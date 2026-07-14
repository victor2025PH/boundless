# 运行与测试说明

## 启动主机 API

在**项目根目录**（含 `server.py`、`src/`、`config/`）执行：

```bash
python server.py
```

默认监听 `http://0.0.0.0:18080`（或环境变量 `OPENCLAW_PORT`）。若端口被占用，可改环境变量或 `src/openclaw_env.py` 中的 `DEFAULT_OPENCLAW_PORT`。

## 端到端测试（主机执行任务）

1. 手机 USB 连接电脑，执行 `adb devices` 确认有一台 device。
2. 启动服务：`python server.py`（在项目根）。
3. 执行测试脚本（PowerShell，项目根）：

   ```powershell
   .\scripts\run_tests.ps1
   ```

   脚本会：拉取设备列表 → 创建一条 Telegram 发送任务（主机执行）→ 等待后查询任务结果。

## 手机 Agent 测试（主机上模拟）

1. 在 `openclaw_agent` 目录下已有 `config.yaml`（或复制 `config.example.yaml` 为 `config.yaml`）。
2. 配置 `host_url: "http://127.0.0.1:18080"`、`device_id: "<手机序列号>"`、`use_host_adb: true`。
3. 创建仅下发的任务（不主机执行）：
   ```powershell
   $body = '{"type":"telegram_send_message","device_id":"89NZVGKFD6BYUO5P","params":{"username":"@ykj123","message":"Agent test"},"run_on_host":false}'
   Invoke-RestMethod -Uri "http://127.0.0.1:18080/tasks" -Method Post -Body $body -ContentType "application/json"
   ```
4. 在 `openclaw_agent` 目录运行 agent：`python agent.py`。Agent 会轮询并拉取该任务、用主机 ADB 执行、再 PUT 上报结果。
5. 查询任务：`GET http://127.0.0.1:18080/tasks/<task_id>`，应看到 `status: completed` 和 `result`。

## 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /devices | 设备列表 |
| GET | /devices/{id}/status | 设备状态与当前 Activity |
| POST | /tasks | 创建任务（body 可含 run_on_host: false 仅下发） |
| GET | /tasks?device_id=&status=pending | 按设备拉取待执行任务 |
| GET | /tasks/{id} | 任务详情 |
| PUT | /tasks/{id}/result | 上报执行结果（手机 agent 用） |
