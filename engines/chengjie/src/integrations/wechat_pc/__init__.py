# -*- coding: utf-8 -*-
"""个人微信 · PC 副驾（实施97 线 B，准入区）。

路线：**PC 微信 4.x + Windows UI Automation 读屏 + 模拟键入**——不注入、不改客户端、不碰协议、
不接触凭证（登录由主人手机扫码完成）。2026-09-07 本机探针（``scripts/wechat_pc_probe.py``）
证实微信 4.1.12 的窗口暴露完整语义控件树（``mmui::*`` 类名 + 层级 AutomationId + Name），
UIA 路线成立（对比：LINE for PC 零语义句柄）。

与统一收件箱的接法**复用桌面桥**（``mode="desktop"``，与 Electron 壳内嵌网页账号同一条路）：
入站 ``POST /api/desktop/ingest``、出站 ``GET /api/desktop/outbound`` → 守卫发送 → ``POST
/api/desktop/outbound/ack``；``DesktopOutboundQueue.enqueue()`` 内建 Kill-Switch/发送闸门，
autosend 对 desktop 账号的路由、列表/线程按 store 读出等全部现成。平台键 ``wechat``（与企微
客服 ``wechat_kf`` 严格分开）。

模块分工（纯逻辑全部可离线单测，真机绑定隔离在 ``uia_backend``）：
- ``policy``       三档形态（副驾 / 半自动 / 全自动仅回复）+ 永不清单 + 日配额/工作时间
- ``identity``     会话身份稳定（微信号 > 昵称+头像哈希），备注改名不裂会话
- ``risk_screens`` 风控/掉线弹窗分类 → 急停 TTL / 离线告警
- ``send_guard``   五步发送守卫（切会话→校标题→填入→校输入框→回车→读回气泡），任一失败冻结
- ``backend``      驱动协议 + 假后端；``uia_backend`` 真机绑定（锚点表按探针产物维护）
- ``service``      轮询骨架：登录/弹窗巡检 → 未读扫描 → 读新气泡 → 入站；拉出站 → 守卫发送 → 回执
"""
from __future__ import annotations

PLATFORM = "wechat"
BRIDGE_MODE = "desktop"       # 注册表 mode（复用桌面桥；见模块头）
BRIDGE_KIND = "pcui"          # 驱动形态标记（日志/状态/将来 meta.bridge）

__all__ = ["PLATFORM", "BRIDGE_MODE", "BRIDGE_KIND"]
