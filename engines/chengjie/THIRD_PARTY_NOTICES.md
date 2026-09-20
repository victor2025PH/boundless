# 第三方开源组件声明（Third-Party Notices）

智聊 ChatX 桌面版随包分发以下开源组件。各组件按其原许可证使用；本文件与安装目录
`resources/services/<组件>/` 内的 LICENSE/NOTICE 一并构成许可声明。

## 需要特别声明的 Copyleft 组件

### qq-personal-connector（QQ 个人号连接边车） — GPL-2.0

- 位置：`resources/services/qq-personal/`（源码形态随包）
- 来源：注入 / QQNT hook / 内核调用层移植改造自 [LLOneBot](https://github.com/LLOneBot/LLOneBot)
  v4.9.4（直连注入形态）；Milky 协议适配层移植自 [LuckyLilliaBot](https://github.com/LLOneBot/LuckyLilliaBot)
  `src/milky`。两者均为 GNU GPL-2.0。
- 我们的修改：去除 OneBot 11 / Satori 适配，去除一切外部签名服务与 auth token 依赖（签名由本机
  QQ 客户端自身完成），只保留智聊所需的 Milky API 子集，新增 `x_*` 扩展与 `/health`。
  详见 `services/qq-personal/NOTICE.md`。
- 许可要求的履行：
  - GPL-2.0 全文随包（`services/qq-personal/LICENSE.GPL-2.0.txt`）；
  - 对应源码随包（同目录即源码，未混淆最小化）并提供书面要约（`services/qq-personal/SOURCE_OFFER.md`）；
  - 该组件作为**独立进程**运行，与智聊主体（Python 后端 / Electron 壳）仅经本机 HTTP/WebSocket
    通信，无链接、无同进程加载——智聊主体不构成其衍生作品。
- 说明：该组件驱动的是**用户自己安装的** QQ 客户端（`QQ.exe` 属腾讯，不随本软件分发；
  未安装时由用户在产品内从腾讯官方地址按需下载）。

## 其它随包边车的主要依赖（宽松许可）

| 组件 | 许可 | 用途 |
|---|---|---|
| @whiskeysockets/baileys | MIT | WhatsApp 协议多开边车 |
| playwright-core | Apache-2.0 | Messenger 托管登录边车 |
| express / ws / pino / qrcode | MIT | 边车 HTTP/WS/日志/二维码 |
| Electron | MIT | 桌面壳与边车运行时 |
| FFmpeg（随包 ffmpeg.exe/ffprobe.exe） | LGPL-2.1+ / GPL（按构建） | 音视频换封装与探测 |

完整依赖树的许可清单可在各 `services/*/package-lock.json` 与 `desktop/package-lock.json` 中查得。
