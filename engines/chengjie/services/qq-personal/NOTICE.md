# qq-personal-connector · 第三方来源与修改声明

本边车是智聊 ChatX 的**自研** QQ 个人号协议连接组件。它把自己作为 QQNT 插件注入本机
安装的 QQ 客户端、驱动 QQ 内核（`wrapper.node`）收发消息——**消息签名由 QQ 客户端
自身完成**，不连接任何外部签名服务、不需要任何第三方 token 或审核。对上以
[Milky 协议](https://milky.ntqqrev.org/)（HTTP + WebSocket）暴露，供智聊后端
`src/integrations/qq_milky.py` 对接。

## 移植来源（均为 GPL 家族，允许商用，须同许可再发布 + 附源码获取）

| 组件 | 来源项目 | 许可 | 用途 |
|---|---|---|---|
| QQNT 注入 / IPC hook / 内核调用（`ntqq/`） | [LLOneBot](https://github.com/LLOneBot/LLOneBot) v4.9.4（直连注入形态，无 PMHQ、无签名服务、无 token） | GPL-2.0 | 拦截 QQNT 的收发/撤回/已读/群管理调用，驱动本机 QQ |
| Milky 协议适配（`milky/`） | [LuckyLilliaBot](https://github.com/LLOneBot/LuckyLilliaBot) `src/milky` | GPL-2.0 | 段类型转换、`/api`+`/event` 服务 |

## 我们的修改

1. **只保留智聊用到的 Milky API 子集**（见 `server.js` 顶部清单），其余返回 `-404`。
2. **去掉 OneBot 11 / Satori 适配**——智聊只走 Milky。
3. **去掉一切外部签名 / auth token 依赖**：不引入 LLBot v8 的 `sign-proxy` 与
   `auth.luckylillia.com` 校验；不引入 PMHQ 闭源注入器。签名走本机 QQ 内核。
4. 新增扩展 API `x_get_login_qrcode` / `x_quick_login_list` / `x_logout` 与 `/health`
   （报 `qq_installed` / `qq_version` / `login_state` / `download_progress`），供智聊
   连接弹窗与就绪诊断消费。
5. 会话凭据落用户可写数据根，绝不随安装包分发（见 `desktop/build/after-pack.js` FORBIDDEN）。

## 当前形态

本目录当前提供 **`server.js` 的 Milky 服务外壳 + 可注入驱动接口 `ntq/driver.js`**：
- 默认驱动为 `mock`（离线自测：假登录、假收发，供 CI 与端到端门禁跑通）；
- 真实注入驱动 `ntq/qqnt-driver.js`（GPL-2.0 移植）在去风险验证通过后接入，
  与外壳的接口契约已冻结（见 `ntq/driver.js` 的 JSDoc）。
