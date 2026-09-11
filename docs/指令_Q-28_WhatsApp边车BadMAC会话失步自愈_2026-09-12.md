# 指令 Q-28：WhatsApp 边车 libsignal「Bad MAC」会话失步自愈 + 入站丢失可见

> 预算 **$80**。工单 **#279（P1，JS63M2；09-10 18:31 skuio 1.0.79 起）**。归属：`services/whatsapp-baileys`（边车）+ Python 入站桥 + 会话头提示。与 Q-24（Messenger 边车）互不重叠。落点表 `docs/发版对账_v1.0.83_Q28.md`。

## 事实（证据 `tmp_diag\JS63M2`）

- wa-sidecar 反复 `Session error: Bad MAC`（libsignal `verifyMAC`），发送方 227500391174370——该联系人入站疑被**静默丢弃**（解密失败的消息没有进入站桥、没有任何界面提示）。同报告还有 WA DNS 抖动 / 重连日志，是另一件（网络）。
- Baileys 口径：`Bad MAC` = 本地 Signal 会话与对端失步（对端换机 / 重装 / 我方 creds 回滚）；标准处置是对该 JID 发 **retry receipt** 让对端重发 + 必要时 `assertSessions` 重建会话；若我方 `creds.json` 被旧备份覆盖会持续失步。

## 要做的四段

| 段 | 内容 | 文件 |
|---|---|---|
| **A 自愈** | 捕获 `Bad MAC` / `No session` / `Invalid PreKey ID` 三类解密错：① 对该 JID 发 retry receipt（Baileys `sendRetryRequest` / `getMessage` 回调接线）；② 同 JID 连续 ≥3 次 → `assertSessions([jid], true)` 重建会话；③ 仍失败 → 该联系人标 `decrypt_failed` 状态并停止无限重试（每 10 分钟一次），日志 `[wa] decrypt_fail jid= kind= attempt= action=` | `services/whatsapp-baileys`（消息处理 / 错误处理） |
| **B 可见** | 解密失败的消息以「占位入站」进会话（气泡「一条消息无法解密 · 已请求对方重发」，不起草不计强未读）；连续失败 → 会话头黄条「WhatsApp 加密会话失步 · 已自动修复中 / 请让对方重发一条」；`reply_diagnosis` 新 finding `wa_decrypt_fail`；`why_no_reply` 同步 | Python 入站桥、`unified_inbox.html`（`wd-` 前缀）、`reply_diagnosis.py`、i18n |
| **C creds 保护** | 边车启动时若 `creds.json` 比 `sessions/` 目录新（或反之落后 >1 天）落 WARNING「凭据与会话可能回滚」；`sessions` 备份只增不覆盖 | 边车启动、备份脚本 |
| **D 门禁** | 边车 node 测试：模拟 Bad MAC → retry receipt 发出、第 3 次 assertSessions、第 N 次停止；Python：占位入站落库 + finding；回放 JS63M2 序列 | tests |

## 红线

- 不动 Messenger 边车（Q-24）与发送闸；只处理 WA 入站解密路径。
- 绝不静默丢弃：任何解密失败要么修复要么可见。

## 开新对话粘贴

```
读 docs/指令_Q-28_WhatsApp边车BadMAC会话失步自愈_2026-09-12.md，按它做 Q-28（#279 P1：whatsapp-baileys 捕获 Bad MAC / No session / Invalid PreKey 三类解密错 → 对该 JID 发 retry receipt，同 JID 连续 ≥3 次 assertSessions 重建，仍失败标 decrypt_failed 每 10 分钟一次并落 [wa] decrypt_fail 日志 / 解密失败以占位入站进会话「一条消息无法解密 · 已请求对方重发」不起草，连续失败会话头黄条 + reply_diagnosis wa_decrypt_fail + why_no_reply / 启动核 creds.json 与 sessions 时间戳回滚 WARNING，备份只增不覆盖 / 边车 node 测试 + Python 占位入站 + JS63M2 回放）。预算 $80。先读 tmp_diag\JS63M2 报告与 services/whatsapp-baileys 消息处理入口，再 scripts\agent_probe.ps1 -Intent 登记；不动 Messenger 边车与任何发送闸；绝不静默丢弃。每个 commit 带 #279 并立刻补落点表 docs/发版对账_v1.0.83_Q28.md 一行；git add 显式路径且提交前 git diff --cached --name-only 为空。收工只改 docs/Q批_进度总账.md 的 Q-28 行并 agent_probe -Done。
```
