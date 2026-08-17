# -*- coding: utf-8 -*-
"""双面板融合（surface fusion）P0：能力注册表 + 驾驶权互斥锁（2026-08-13）。

背景＝同一账号有两个操作面（surface）：
- **workspace**＝统一收件箱（自建 UI + 服务器端 sidecar，如 messenger-web Playwright）；
- **native**＝桌面壳内嵌官方网页标签（webview + shared/inject 注入层，assist 档）。

两面板共享同一个后端大脑（AI/翻译/审计/去重），本模块补三块地基：

1. **能力注册表**（``capability_matrix``）——「平台 × 面板 × 能力」单一事实源。
   两个面板的按钮/徽章由它驱动：能做的亮起、做不到的显示「去另一面板 ↗」桥接
   入口，功能永远不静默消失。P0 只策划 Messenger（融合首个目标平台）；其余平台
   返回空表=前端不渲染，后续逐平台补。状态语义（门禁钉住）：
   - workspace: ``ok``（本面板可用）/ ``bridge``（跳原生面板）/ ``none``（暂无）
   - native:    ``ok``（官方原生天然有）/ ``assist``（注入辅助档）/ ``none``
2. **驾驶权互斥锁**（pilot lock）——每账号唯一「自动化持有者」（workspace|native）。
   今天防双发靠「原生页刻意不开全自动」这个约定；锁把它升级为服务端机制：
   AutosendWorker 投递前经 ``autosend_blocked`` 让位（owner=native → 取消草稿，
   与 send_blocked 同族语义），将来原生页受控出站（P2）上线时反向同理。
   落盘 ``<config_dir>/surface_pilot.json``（mtime 失效缓存，与 notify_webhooks_store
   同哲学：手改文件下次读取即见）；带 capped 审计历史。**默认 owner=workspace**
   ——与今天的实际行为完全一致，锁未启用/无记录时零行为变更。
3. **总开关** ``surface_fusion.enabled``（默认 false，新子系统纪律）——只闸「锁的
   强制执行 + 工作台徽章 UI」；能力注册表/跨面跳转按钮不依赖它（跳转无风险，
   注册表只读）。开关经 config overlay 热重载生效（消费方均为闭包活读）。

线程安全：进程内模块锁；读走 mtime 缓存零热路开销。所有对外函数 fail-open
（异常＝不拦截/空表），锁故障绝不闸死自动回复——与 work_hours_gate 同纪律。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.surface_fusion")

SURFACE_WORKSPACE = "workspace"
SURFACE_NATIVE = "native"
_VALID_OWNERS = (SURFACE_WORKSPACE, SURFACE_NATIVE)

_PILOT_FILE = "surface_pilot.json"
_HISTORY_CAP = 50

_lock = threading.Lock()
# mtime 失效缓存：{"path": str, "mtime": float, "data": dict}
_cache: Dict[str, Any] = {"path": "", "mtime": -1.0, "data": None}


# ── 能力注册表 ────────────────────────────────────────────────────────────────
# 行契约（tests/test_surface_fusion.py 钉住）：
#   (capability_id, workspace_status, native_status, bridge_to, phase)
#   - workspace_status ∈ {ok, bridge, none}；native_status ∈ {ok, assist, none}
#   - workspace=bridge → bridge_to 必须指向 native（跳转目标必须存在）
#   - 每个 capability_id 必须有 i18n 标签键 sf.cap.<id>（zh+en，pack surface_fusion）
# 内容口径＝docs/平台能力矩阵.md（sidecar 实测）+ shared/inject/profiles.js
# （注入 assist 档）+ 融合方案能力矩阵页；改 sidecar/注入能力时同步本表。
_MESSENGER_CAPS = (
    # 双面板已对齐
    ("send_text", "ok", "ok", "", "P0"),
    ("send_media", "ok", "ok", "", "P0"),
    ("translate_text", "ok", "assist", "", "P0"),
    ("translate_media", "ok", "assist", "", "P0"),
    ("ai_draft", "ok", "assist", "", "P0"),
    ("message_requests", "ok", "ok", "", "P0"),
    ("e2ee_pin", "ok", "ok", "", "P0"),
    # 全自动：workspace 独有（native 受控出站属 P2，上线前 native=none 是诚实态）
    ("autosend", "ok", "none", "", "P0"),
    # 已读回执（P3 真相翻转 2026-08-13，零代码）：sidecar 轮询实时入站会打开线程读全文，
    # 而「Messenger 打开线程 = FB 侧标已读」（msg_ops.canOpenThread 文档 + 回填开未读致
    # 「已读不回」的生产实锤）——客户视角已读回执**本就在发生**（约一个轮询周期内）。
    # 无独立开关/操作，是轮询架构的固有行为，注册表如实反映。
    ("mark_read", "ok", "ok", "", "P3"),
    # 正在输入：Messenger 网页版无 presence API，唯一实现是往输入框敲字再删的 DOM hack
    # （风控高危）——刻意不做，workspace=none 是诚实态（P3 复核维持）。
    ("typing", "none", "ok", "", "P1"),
    # 表情回应出站（P3 2026-08-13 已补齐）：/react 端点（每账号写操作互斥锁内）——
    # 面板白名单（😂→😆 归一、面板外诚实拒绝）+ 文本锚定气泡（宁缺勿滥）+ hover→面板
    # 点击（字符/aria 双轨）。失败如实 ok:false（表情失败≠发消息失败，绝不静默装成功）。
    ("reaction_out", "ok", "ok", "", "P3"),
    # 引用回复（2026-08-14 工作台入口整体下线，老板拍板）：发送链存在多个静默降级点、
    # 镜像无条件渲染引用条 →「坐席见引用、客户端没有」（173 实录）。后端 reply_to 透传
    # 链保留（Node 引用态发送逻辑未拆），但 unified_inbox 不再渲染「引用」按钮 →
    # workspace=none 是诚实态；原生页自带引用不受影响。恢复前置＝quote_applied 回执。
    ("quote_reply", "none", "ok", "", "P2"),
    # 桥接解决（低频管理动作：跳原生页一键直达，不重造）
    ("forward", "bridge", "ok", SURFACE_NATIVE, "P1"),
    ("pin_manage", "bridge", "ok", SURFACE_NATIVE, "P0"),
    ("report", "bridge", "ok", SURFACE_NATIVE, "P0"),
    # 永久桥接（WebRTC 实时媒体，sidecar 技术上不可能）
    ("calls", "bridge", "ok", SURFACE_NATIVE, "P0"),
)

# Telegram / WhatsApp（融合第二批，2026-08-13）：workspace＝协议 worker（TG pyrogram /
# WA Baileys）+ 统一收件箱；native＝壳内嵌 web.telegram.org/k/ / web.whatsapp.com
# （shared/inject **完整定制档**，canIngest=true，桌面受控出站链在——与 Messenger 的
# assistOnly 工厂档不同，native 侧自动化真实存在＝autosend native=assist，这正是
# tg/wa 比 msgr 更需要驾驶权锁的原因：两面都能发，锁不闭合＝双发）。
# workspace 列口径＝account_orchestrator worker 方法实测（平台能力矩阵同源）：
# TG worker send() **无 reply_to 形参**（编排器逐级降级到裸发＝引用被静默丢弃）→
# quote_reply workspace=none；WA send(reply_to=quoted) 真透传 → ok；两平台 worker
# 均无表情回应方法 → reaction_out workspace=none（P2＝未排期，原生页先顶）。
_TELEGRAM_CAPS = (
    ("send_text", "ok", "ok", "", "P0"),
    ("send_media", "ok", "ok", "", "P0"),
    ("translate_text", "ok", "assist", "", "P0"),
    ("translate_media", "ok", "assist", "", "P0"),
    ("ai_draft", "ok", "assist", "", "P0"),
    ("autosend", "ok", "assist", "", "P0"),
    ("mark_read", "ok", "ok", "", "P0"),
    ("typing", "ok", "ok", "", "P0"),
    ("reaction_out", "none", "ok", "", "P2"),
    # 引用回复（2026-08-14 工作台入口整体下线，老板拍板）：08-13 P3 补齐 worker
    # reply_to→pyrogram 透传后，仍发生「工作台显示引用、手机端没有」（173 实录 19:01）
    # ——镜像无条件写引用+链路零回执，无法自证引用真上了线。UI 入口已删 →
    # workspace=none 是诚实态；worker send(reply_to=) 代码保留。恢复前置＝quote_applied 回执。
    ("quote_reply", "none", "ok", "", "P3"),
    ("forward", "bridge", "ok", SURFACE_NATIVE, "P0"),
    ("pin_manage", "bridge", "ok", SURFACE_NATIVE, "P0"),
    ("report", "bridge", "ok", SURFACE_NATIVE, "P0"),
    ("calls", "bridge", "ok", SURFACE_NATIVE, "P0"),
)

_WHATSAPP_CAPS = (
    ("send_text", "ok", "ok", "", "P0"),
    ("send_media", "ok", "ok", "", "P0"),
    ("translate_text", "ok", "assist", "", "P0"),
    ("translate_media", "ok", "assist", "", "P0"),
    ("ai_draft", "ok", "assist", "", "P0"),
    ("autosend", "ok", "assist", "", "P0"),
    ("mark_read", "ok", "ok", "", "P0"),
    ("typing", "ok", "ok", "", "P0"),
    ("reaction_out", "none", "ok", "", "P2"),
    # 引用回复（2026-08-14 工作台入口整体下线，老板拍板，三平台同刀）：WA Baileys
    # quoted 透传本身可用，但工作台统一收件箱不再提供引用入口 → workspace=none；
    # 原生页自带引用不受影响。worker send(reply_to=) 代码保留，恢复=还原 UI 单点闸。
    ("quote_reply", "none", "ok", "", "P0"),
    ("forward", "bridge", "ok", SURFACE_NATIVE, "P0"),
    ("pin_manage", "bridge", "ok", SURFACE_NATIVE, "P0"),
    ("report", "bridge", "ok", SURFACE_NATIVE, "P0"),
    ("calls", "bridge", "ok", SURFACE_NATIVE, "P0"),
)

_REGISTRY: Dict[str, tuple] = {
    "messenger": _MESSENGER_CAPS,
    "telegram": _TELEGRAM_CAPS,
    "whatsapp": _WHATSAPP_CAPS,
}

# 可内嵌官方网页的平台（镜像 desktop/renderer/renderer.js::EMBEDDABLE；
# 壳侧收到 openEmbedded 后仍二次校验，这里只是给 API 消费方一个只读清单）。
EMBEDDABLE_PLATFORMS = ("telegram", "whatsapp", "instagram", "messenger", "x", "zalo")


def capability_matrix(platform: str) -> List[Dict[str, Any]]:
    """某平台的「能力 × 双面板」注册表；未策划平台返回空表（前端不渲染）。"""
    rows = _REGISTRY.get(str(platform or "").lower(), ())
    out: List[Dict[str, Any]] = []
    for cap_id, ws, native, bridge_to, phase in rows:
        out.append({
            "id": cap_id,
            "workspace": ws,
            "native": native,
            "bridge_to": bridge_to,
            "phase": phase,
            "label_key": f"sf.cap.{cap_id}",
        })
    return out


def curated_platforms() -> List[str]:
    """已有策划注册表的平台清单。"""
    return sorted(_REGISTRY.keys())


# 能力分组：把 16 项能力按「两条链各能做什么」归四组，给前端能力总览浮层与
# 「原生页独有能力」提示单一口径（否则前端各算一套，与注册表口径易漂移）。
#   both          —— 两个面板都能（工作台 ok + 原生 ok/assist）
#   workspace_only —— 只有工作台能（如全自动 autosend；原生 assist 档刻意不开）
#   native_only    —— 只有原生页能（工作台待补：已读/输入中/表情/引用——P1 backlog）
#   bridge         —— 工作台跳原生页（转发/置顶/举报/通话——低频或技术不可能）
def capability_summary(platform: str) -> Dict[str, List[str]]:
    """某平台能力按双面板归属分四组（纯函数，未策划平台返回四个空组）。"""
    both: List[str] = []
    workspace_only: List[str] = []
    native_only: List[str] = []
    bridge: List[str] = []
    for r in capability_matrix(platform):
        ws, native = r["workspace"], r["native"]
        cid = r["id"]
        if ws == "bridge":
            bridge.append(cid)
        elif ws == "ok" and native in ("ok", "assist"):
            both.append(cid)
        elif ws == "ok" and native == "none":
            workspace_only.append(cid)
        elif ws == "none" and native == "ok":
            native_only.append(cid)
        # 其余组合（如 ws=none & native=assist）刻意不归任何组：既非「两边都行」
        # 也非「一边独有」，前端浮层不展示，避免造出误导性的中间态。
    return {
        "both": both,
        "workspace_only": workspace_only,
        "native_only": native_only,
        "bridge": bridge,
    }


# ── 配置开关 ──────────────────────────────────────────────────────────────────

def fusion_cfg(root_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = (root_cfg or {}).get("surface_fusion")
    return cfg if isinstance(cfg, dict) else {}


def fusion_enabled(root_cfg: Optional[Dict[str, Any]]) -> bool:
    return bool(fusion_cfg(root_cfg).get("enabled", False))


# ── 驾驶权互斥锁（pilot lock）────────────────────────────────────────────────

def _pilot_path() -> Path:
    """锁文件绝对路径（可写数据区优先——与 global_rules overlay 同一唯一事实源，
    生产落实例数据根、测试经 conftest 的 AITR_DATA_DIR 天然隔离进 tmp）。"""
    from src.licensing.data_paths import config_dir
    return config_dir() / _PILOT_FILE


def pilot_key(platform: str, account_id: str = "") -> str:
    return f"{str(platform or '').lower()}:{str(account_id or 'default')}"


def _load() -> Dict[str, Any]:
    """读锁文件（mtime 失效缓存；文件缺失/坏 JSON → 空结构，绝不抛）。"""
    path = _pilot_path()
    spath = str(path)
    try:
        mtime = os.path.getmtime(spath)
    except OSError:
        mtime = -1.0
    with _lock:
        if (_cache["path"] == spath and _cache["mtime"] == mtime
                and _cache["data"] is not None):
            return _cache["data"]
    data: Dict[str, Any] = {"pilots": {}, "history": []}
    if mtime >= 0:
        try:
            with open(spath, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                pilots = raw.get("pilots")
                history = raw.get("history")
                data["pilots"] = pilots if isinstance(pilots, dict) else {}
                data["history"] = history if isinstance(history, list) else []
        except Exception:
            logger.warning("surface_pilot.json 解析失败，按空表处理", exc_info=True)
    with _lock:
        _cache.update({"path": spath, "mtime": mtime, "data": data})
    return data


def get_pilot(platform: str, account_id: str = "") -> Dict[str, Any]:
    """当前驾驶权（无记录＝默认 workspace，零行为变更基线）。"""
    key = pilot_key(platform, account_id)
    entry = (_load().get("pilots") or {}).get(key)
    if isinstance(entry, dict) and entry.get("owner") in _VALID_OWNERS:
        return {
            "owner": entry["owner"],
            "since": float(entry.get("since") or 0),
            "by": str(entry.get("by") or ""),
        }
    return {"owner": SURFACE_WORKSPACE, "since": 0.0, "by": ""}


def set_pilot(platform: str, account_id: str, owner: str,
              by: str = "") -> Dict[str, Any]:
    """切换驾驶权（原子写 + capped 审计历史）。owner 非法抛 ValueError。"""
    owner = str(owner or "").strip().lower()
    if owner not in _VALID_OWNERS:
        raise ValueError(f"invalid owner: {owner!r}")
    key = pilot_key(platform, account_id)
    path = _pilot_path()
    with _lock:
        # 锁内重读最新盘面（绕缓存），防并发写互相覆盖
        data: Dict[str, Any] = {"pilots": {}, "history": []}
        try:
            with open(str(path), "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                if isinstance(raw.get("pilots"), dict):
                    data["pilots"] = raw["pilots"]
                if isinstance(raw.get("history"), list):
                    data["history"] = raw["history"]
        except Exception:
            pass
        prev = data["pilots"].get(key) if isinstance(
            data["pilots"].get(key), dict) else {}
        now = time.time()
        data["pilots"][key] = {"owner": owner, "since": now, "by": str(by or "")}
        data["history"].append({
            "ts": now, "key": key, "owner": owner,
            "prev": str((prev or {}).get("owner") or SURFACE_WORKSPACE),
            "by": str(by or ""),
        })
        data["history"] = data["history"][-_HISTORY_CAP:]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, str(path))
        except Exception:
            logger.error("surface_pilot.json 写入失败", exc_info=True)
            raise
        _cache.update({"path": "", "mtime": -1.0, "data": None})  # 下次读走磁盘
    return get_pilot(platform, account_id)


def autosend_blocked(root_cfg: Optional[Dict[str, Any]], platform: str,
                     account_id: str = "") -> bool:
    """工作台自动链投递前的让位判定：fusion 开 且 owner=native → True。

    fail-open：任何异常返回 False（锁故障绝不闸死自动回复）。
    **纯谓词**——不计数（UI/探针也会调它）；让位计数由执行点显式调
    ``note_pilot_yield``（A 线 protocol_autoreply / B 线 bootstrap guard 闭包）。
    """
    try:
        if not fusion_enabled(root_cfg):
            return False
        return get_pilot(platform, account_id).get("owner") == SURFACE_NATIVE
    except Exception:
        logger.debug("autosend_blocked 判定异常（放行）", exc_info=True)
        return False


# ── 让位观测（P4 2026-08-13）：「锁真的在工作」的运行读数 ─────────────────────
# 进程级计数（重启清零；持久口径是草稿 decided_by=pilot_native 审计行）。B 线
# （AutosendWorker）已有 total_skipped_pilot，但 A 线（protocol_autoreply 直发链，
# ChatX 默认档的主发送链）此前让位零观测——两条链让位都记到这里，
# /api/surface/capabilities 的 pilot_yields 一处看全。
_yields: Dict[str, Dict[str, Any]] = {}


def note_pilot_yield(platform: str, account_id: str = "",
                     chain: str = "autosend") -> None:
    """记一次驾驶权让位（chain: a_line|autosend）。best-effort，绝不抛。"""
    try:
        key = pilot_key(platform, account_id)
        with _lock:
            ent = _yields.setdefault(
                key, {"count": 0, "chains": {}, "last_ts": 0.0})
            ent["count"] = int(ent["count"]) + 1
            ch = str(chain or "unknown")
            ent["chains"][ch] = int(ent["chains"].get(ch, 0)) + 1
            ent["last_ts"] = time.time()
    except Exception:
        pass


def pilot_yield_stats() -> Dict[str, Any]:
    """全部账号的让位计数快照（无敏感字段）。"""
    with _lock:
        return {
            k: {"count": v["count"], "chains": dict(v["chains"]),
                "last_ts": v["last_ts"]}
            for k, v in _yields.items()
        }


def _reset_cache_for_tests() -> None:
    with _lock:
        _cache.update({"path": "", "mtime": -1.0, "data": None})
        _yields.clear()
