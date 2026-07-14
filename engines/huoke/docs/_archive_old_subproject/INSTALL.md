# OpenClaw 安装说明

OpenClaw 分为 **主机**（发任务、收结果）和 **手机**（领任务、本机操作、上报结果）。先装主机，再装每台手机。

## 快速步骤

### 主机（PC / 服务器）

1. 进入项目目录，安装依赖并启动：
   ```bash
   cd mobile-auto-project
   pip install -r requirements.txt
   python server.py
   ```
2. 记下本机局域网 IP（如 `192.168.1.100`），手机配置里要填 `http://<该IP>:8000`。
3. 若本机有防火墙，放行 8000 端口。

### 手机（每台）

1. 安装 **Termux**（建议 F-Droid），在 Termux 里执行：
   ```bash
   pkg update && pkg install python android-tools
   ```
2. 手机设置里开启 **无线调试**，在 Termux 里完成本机 ADB 自连：
   ```bash
   adb pair 127.0.0.1:<配对端口> <配对码>
   adb connect 127.0.0.1:<无线调试端口>
   adb devices   # 应看到 127.0.0.1:xxxxx device
   ```
3. 把项目里的 **openclaw_agent** 目录拷到手机（U 盘 / adb push / git 等），进入目录：
   ```bash
   cd ~/openclaw_agent
   pip install -r requirements.txt
   cp config.example.yaml config.yaml
   ```
4. 编辑 **config.yaml**：填 `host_url`（主机地址）、`adb_port`（无线调试端口）；`device_id` 可留空，程序会在首次连接 ADB 时自动读取本机序列号。
5. 运行：`python agent.py`（常驻可用 `nohup python agent.py &`）。

## 详细流程与协同方式

- **完整安装步骤**（含防火墙、多机、自检）：见 **[docs/OPENCLAW_INSTALL_AND_SYNC.md](docs/OPENCLAW_INSTALL_AND_SYNC.md)**。
- **主机与手机如何通信、任务如何下发与上报**：同上文档中的「主机与手机如何沟通、协同工作」一节。

安装完成后，在主机上通过 `POST /tasks` 创建任务（指定 `device_id` 或留空），手机会在轮询时自动领任务、执行并上报结果。
