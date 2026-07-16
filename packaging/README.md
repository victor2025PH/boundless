# packaging/ · 全域打包与分发索引

> 同 deploy/：索引各产品/引擎既有打包链，不搬迁（避免破坏相对路径）。

| 产物 | 位置 | 说明 |
|---|---|---|
| 桌面控制台 exe | `engines/avatarhub/build_launcher.bat` + `*.spec`（PyInstaller） | `dist/AvatarHub.exe` |
| Windows 安装包 | `engines/avatarhub/installer/AvatarHub.iss`（Inno Setup） | 薄核心：exe+脚本+前端+依赖基线+文档，绝不打包 env/模型/机密 |
| 分发清单/上传 | `engines/avatarhub/`（make_release/make_portable/publish_release/upload_release） | 渠道清单自动化 |
| 官网构建 | `website/`（`npm run build`） | 部署到 bd2026 |

## 待办（Phase 7）
- 按产品出 SKU 化交付包（对应 `products/*/product.yaml#delivery`）：如 tongyi 轻量翻译包、huanying 数字人包。
- Windows 代码签名证书（消除 SmartScreen 告警）；Mac Developer ID 公证（见 docs 三视角方案）。
