# 手机端 OpenClaw Agent（Termux + 本机 ADB）

在手机上的 Termux 里运行，轮询主机任务 → 调用 AI（可选）→ 用本机 ADB 操作手机 → 上报结果。

## 前置条件

1. Android 11+，开启开发者选项与**无线调试**。
2. 安装 [Termux](https://termux.dev/)（建议 F-Droid 安装）。
3. Termux 内安装：`pkg update && pkg install python android-tools`
4. 本机 ADB 自连（在 Termux 执行一次，重启后需重做）：
   - 设置 → 开发者选项 → 无线调试 → 记下端口与配对码
   - `adb pair 127.0.0.1:<配对端口> <配对码>`
   - `adb connect 127.0.0.1:<无线调试端口>`
   - `adb devices` 应能看到本机

## 配置

复制 `config.example.yaml` 为 `config.yaml`，填写：

- `host_url`: 主机任务 API 地址（如 `http://192.168.1.100:8000`）
- `device_id`: 本机标识（与主机侧一致）；可留空，程序会在本机 ADB 连接成功后自动读取序列号
- `adb_host`: 本机 ADB 地址，通常 `127.0.0.1`
- `adb_port`: 无线调试端口

## 运行

```bash
cd openclaw_agent
pip install -r requirements.txt
python agent.py
```

后台常驻可用 `nohup python agent.py &` 或 Termux 的 run-boot 等。

## 主机侧要求

- `GET /tasks?device_id=xxx&status=pending` 返回该设备待执行任务。
- `PUT /tasks/{task_id}/result` 或 `PATCH` 接收 body `{ "success": true/false, "error": "", "screenshot_path": "" }` 用于上报结果，任务才不会“停住”不更新。

详见项目根目录 `docs/PHONE_OPENCLAW_FEASIBLE_SOLUTION.md`。
