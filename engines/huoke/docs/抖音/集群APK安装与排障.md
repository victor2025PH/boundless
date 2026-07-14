# 集群 APK 安装与排障

> 主控浏览器向 Worker 手机批量安装 APK 的说明与常见问题。

## 功能说明

- **本机 USB**：使用 `POST /batch/install-apk`，手机须插在运行 OpenClaw 的同一台电脑。
- **集群 Worker**：使用 `POST /batch/install-apk-cluster`（与 `POST /cluster/batch/install-apk` 等价），由主控把 APK 转发到各 Worker 再 `adb install`。

## 自检

1. 浏览器访问：`GET /health`，确认返回中含：
   - `capabilities.post_batch_install_apk_cluster: true`
   - `capabilities.post_cluster_batch_install_apk: true`
2. 若字段缺失：主控进程未加载最新代码，需**重新部署并重启**。
3. 可选：环境变量 `OPENCLAW_BUILD_ID` 用于区分构建（会出现在 `build_id`）。Windows 可在仓库根目录执行：`. .\scripts\set_build_id_env.ps1`（详见脚本内说明）。

### OpenAPI 与面板内说明

- `GET /openapi.json` 中应存在 `POST /batch/install-apk-cluster` 与 `POST /cluster/batch/install-apk`（用于确认路由已注册）。
- 控制台 **投屏区「安装 APK」** 面板展开 **「排障与说明」** 可快速对照反代路径与 `/health` 字段（与本文一致）。

## 反向代理

须放行（至少）：

- `POST /batch/install-apk`
- `POST /batch/install-apk-cluster`
- `POST /cluster/batch/install-apk`（备用路径，前端在首路径 404 时会自动重试）

仅放行 `/api` 前缀时，请把上述路径纳入同一后端。

## 鉴权

若启用 `OPENCLAW_API_KEY`，主控转发 Worker 时会带 `X-API-Key`，**各节点密钥需一致**。

## 日志

主控日志关键字：`[cluster_apk]`，含 `forward`、`worker_ok`、`forward_error`，便于区分主控问题与 Worker 问题。
