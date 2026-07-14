# GitHub 上传清单

本仓库是 OpenClaw 手机集群自动化系统的主仓。上传 GitHub 时只提交可复现的工程资产，不提交本机运行数据。

## 应提交

- `src/`：业务代码、RPA 执行、后台 API、前端静态资源。
- `tests/`：单元测试、回归测试、契约测试。
- `docs/`：架构、部署、运维、操作、市场说明中已脱敏的文档。
- `scripts/`、`tools/`：长期可复用的部署、诊断、测试工具。
- `.github/`：CI 工作流。
- `requirements*.txt`、`Dockerfile`、`docker-compose.yml`、`README.md`。
- `config/*.example.*` 或不含真实设备、账号、代理、密钥的模板配置。

## 不应提交

- `.env`、`config/launch.env`、API key、账号密码、代理账号。
- `data/`、`logs/`、`reports/`、`debug/`。
- `*.db`、`*.db-wal`、`*.db-shm`、运行态 JSON 状态。
- 根目录和调试目录里的截图、UI dump、临时测试输出。
- `apk_repo/` 里的 APK 二进制，改走 Release 或外部下载说明。
- 真实设备序列号、手机号、客户资料、聊天记录截图。

## 提交前检查

```bash
git status --short
git diff --stat
git diff --cached --stat
```

如果看到 `data/`、`logs/`、`debug/`、`.env`、`*.db`、`*.png`、`*.jpg`、真实设备配置或客户数据，先不要提交。
