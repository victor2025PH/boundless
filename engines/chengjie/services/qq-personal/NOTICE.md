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

## 真驱动移植说明（2026-09-10，B 段）

`ntq/qqnt-driver.js` / `ntq/qqnt-agent.cjs` / `ntq/qqnt-ipc.js` / `ntq/qqnt-elements.js` / `ntq/qqnt-versions.js`
是对 **LLOneBot v4.9.4 直连注入形态**（GPL-2.0）的移植，按 GPL-2.0 同许可发布，全部留在本目录：

| 来源（LLOneBot v4.9.4） | 移植到 | 保留 | 修改 |
|---|---|---|---|
| `src/ntqqapi/wrapper.ts`、`hook.ts`（拦 `.node` 加载拿 wrapper 导出；代理 `NodeIQQNTWrapperSession.create` / `NodeIKernelLoginService`；包装 `addKernelXxxListener` 旁路事件） | `qqnt-agent.cjs` | 挂钩位置与监听器包装方式 | 改为零依赖 CommonJS；去掉 OneBot/Satori/HTTP 面，只留本机命名管道 RPC；`API_PROFILES` 按 build 分档 |
| `src/ntqqapi/api/msg.ts` / `file.ts` / `group.ts` / `friend.ts`（`sendMsg` / `recallMsg` / `setMsgRead` / `getRichMediaFilePathForGuild` / `kickMember` / `modifyGroupName` / `approvalFriendRequest` 调用形状） | `qqnt-agent.cjs` `METHODS` | 内核方法名与参数形状 | 只保留 Milky 子集；uid/uin 解析走 `getUidByUinV2` 优先并做缓存 |
| `src/ntqqapi/types/msg.ts`（元素类型号、`picElement` / `fileElement` / `replyElement` 字段） | `qqnt-elements.js` | 元素形状 | 直接与 Milky 段互转；record/video/forward 明确 unsupported 而非静默丢 |
| `src/main/...` 启动注入（改 `resources/app/package.json` main 指向 launcher） | `qqnt-driver.js` `ensureInjected` | 注入方式 | **只对智聊自己下载的运行时 QQ 做**（`source=runtime`），用户自装 QQ 一律拒绝；幂等；备份原 `package.json` |

不含来源、智聊自写：`qqnt-ipc.js`（随机 token 经环境变量传入、只在内存比对）、`qqnt-versions.js` 版本表、
日志纪律（不落消息正文 / token / 二维码 png）、`server.js` 的 `/media/:id` 回放。

## 当前形态

本目录提供 **`server.js` 的 Milky 服务外壳 + 驱动接口 `ntq/driver.js` + 两个实现**：
- `mock`（离线自测：假登录、假收发，供 CI 与端到端门禁跑通）；
- `qqnt`（真驱动，默认）：本机定位到**运行时目录内、版本表内**的 QQ 才注入；任一条件不满足即回退 `mock`，
  `/health` 如实标 `driver="mock" + driver_reason`。真驱动**尚未经真机 72 h 去风险验证**（版本表 `verified` 皆空），
  验证通过前安装包不把它列为 REQUIRED、产品对外口径保持「演示态」（平台注册表 `qq.driver_state=mock`）。
