# P4 第一步：browser-use 只读试点（2026-08-29）

## 结论先说

**这条路走得通，而且边际成本是零。** 现成的 browser-use 用**我们局域网的模型**
就能驱动，不必烧云端 token。

首跑实测（`pilot_readonly.py`，任务＝去官网查智聊能对接哪些聊天平台）：

| 项 | 结果 |
|---|---|
| 判定 | **PASS** |
| 耗时 / 步数 | 87.3s / 6 步 |
| 驱动模型 | 局域网 `173:8001` chatx（Qwen3.6-27B-abl-AWQ），**零云端调用** |
| 答案核对 | **4/4** 命中（Telegram / WhatsApp / LINE / Messenger） |

任务是刻意选的：**答案我们事先知道**，所以 agent 的回答能被逐项核对。选一个
我们不知道答案的任务，跑通了也只能证明「它说了些话」。

它的回答里还主动加了一句「ReachX（智拓）是另一款产品，支持 Facebook /
Messenger / TikTok / Instagram，不属于智聊/ChatX」——说明它真读懂了页面结构，
而不是把页面上所有平台名一把抓。

## 选型：为什么是 browser-use

老板定的原则是「不重新写，直接调用别人做好的源码」。按**许可证**先筛一遍
（这是第一道关，不是能力）：

| 项目 | 许可证 | 结论 |
|---|---|---|
| **browser-use** | MIT | ✅ 本次选它 |
| Terminator (mediar-ai) | MIT | ✅ 第二步（Windows 原生应用走无障碍树） |
| Agent-S | Apache 2.0 | ✅ 可用（planner/grounder 可换我们的模型） |
| UI-TARS | Apache 2.0 | ⚠️ 可用但要独占 ~16GB 显存 |
| **Open Interpreter** | **AGPL-3.0** | ❌ **传染性许可，商业产品不碰** |

顺带一条供应链信息：browser-use 从 0.12.5 起把 `litellm` **移出核心依赖**
（1.82.7/1.82.8 被供应链攻击）。本 spike 确认过没有把它装进来（`pip show
litellm` 为空），直接用 `ChatOpenAI` 连局域网端点即可。

## 踩到并已解决的坑

**`ChatOpenAI` 没有 `extra_body` 参数**，而 chatx 在 vLLM 上默认开 thinking
→ `message.content` 恒为 null、正文全进 reasoning、预算被思考吃光。这个坑同一
天已经咬过两次（局域网翻译兜底静默失效、小智 docless 轮），所以这里在
**httpx 传输层**注入 `chat_template_kwargs.enable_thinking:false`
（`_ThinkingOffTransport`）。它只认这一个 vLLM 专有键，云端/Ollama 端点对未知
字段一律忽略，同发无害。

**注意给后来人**：`browser-use install` 会顺手删掉 `ms-playwright` 下的旧版
浏览器（日志里那句 `Removing unused browser at ...chromium-1194`）。装完务必
复跑一次项目的真浏览器门禁——本次已验证 `tools/verify_assistant_ui.py`
111/111 仍全绿，删的是旧版本，当前门禁用的那份没动。

## 边界（这个试点刻意不做什么）

- **只读**：不登录、不填表、不点提交。写操作要等接进 `XZAgent` 已有的两阶段
  确认 + 撤销 + 审计（那套东西已经在跑，不该另造一套）。
- **独立环境**：本目录自带 `.venv`（已被 `.gitignore` 的 `.venv*/` 覆盖），
  browser-use 拉了 30+ 个包，不进生产依赖树——否则生产重启的风险就和它绑在
  一起了。
- **未接入产品**：这是 spike，不是功能。接入前要先定的是范围与确认模型，
  不是代码。

## 用法

```bash
.venv\Scripts\python.exe pilot_readonly.py            # 局域网模型（默认）
.venv\Scripts\python.exe pilot_readonly.py --cloud    # 云端对照组
.venv\Scripts\python.exe pilot_readonly.py --headed   # 显示浏览器窗口
```

## 下一步建议（按价值排序）

1. **登录态只读任务**：让它进我们自己的后台读一个数（如「这周有多少条未回」）。
   这是坐席真会做的事。需要先定凭据怎么给——建议用一个**只读账号**，而不是把
   管理员密码交给 agent。
2. **接进 XZAgent 的确认框架**：写操作必须走既有的两阶段确认 + 撤销 + 审计。
   这一步是「从玩具到能用」的分界线。
3. **Windows 原生应用（Terminator + UIA）**：浏览器之外的场景。无障碍树比纯
   视觉快一个量级且不受分辨率影响；视觉兜底用我们已有的双活 VLM，不引入
   UI-TARS 的显存开销。
