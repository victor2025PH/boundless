"""托管租户到期提醒读取端（tenant_notice.json → 工作台横幅）。

写入方＝**厂商侧**两个进程外守护：`tenant_ops watch`（每 10min 巡检，按到期账本
写/清）与 `tenant_fulfill_watch`（续费叠期即删旧提醒）。本模块只是租户实例读自己
数据区的一份轻量 JSON，把「服务快到期/已到期 + 续费深链」透传给
`/api/workspace/ai-runtime-status` 的既有 60s 轮询（零新增轮询/路由）。

非托管部署（zhiliao/tongyi 生产、装机版）没有该文件 → 恒 None，零行为变化。

路径刻意用 CWD 相对（C 类数据路径）：读取只发生在**服务进程**里，其启动契约
CWD=实例数据根；从引擎根跑的 CLI 从不读它。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

#: 服务进程 CWD=实例数据根 → config/ 即实例配置区
NOTICE_FILE = Path("config") / "tenant_notice.json"

#: 写入方停摆守卫：written_at 超过 72h（watch 10min 一轮，72h=守护死了 400+ 轮）
#: → 数据可信度已失（客户可能早续费了），宁可不显示也不拿陈旧急迫度吓客户。
STALE_SEC = 72 * 3600


def parse_tenant_notice(data: Any, now: float) -> Optional[Dict[str, Any]]:
    """校验 + 重算（纯函数）：文件载荷 → 横幅载荷；不合格/陈旧 → None。

    ``days_left`` 按 ``expires_at`` 在**读取时点**重算（写入是 10min 节拍，读取是
    60s 轮询——重算比转发写入时的快照新鲜）；重算越过零点自动升级 expired，
    避免「还剩 -0.1 天」的分裂文案。
    """
    if not isinstance(data, dict) or data.get("kind") != "expiry":
        return None
    level = str(data.get("level") or "")
    if level not in ("expiring", "expired"):
        return None
    try:
        written = float(data.get("written_at") or 0)
    except (TypeError, ValueError):
        return None
    if written <= 0 or (now - written) > STALE_SEC:
        return None
    expires_at = str(data.get("expires_at") or "").strip()
    days_left: Optional[float] = None
    if expires_at:
        try:
            exp = time.mktime(time.strptime(expires_at, "%Y-%m-%d %H:%M:%S"))
            days_left = round((exp - now) / 86400, 1)
        except Exception:  # noqa: BLE001 - 格式坏就不给天数，级别沿用写入方
            days_left = None
    if days_left is not None and days_left < 0:
        level = "expired"
    renew_url = str(data.get("renew_url") or "")
    if not renew_url.startswith("https://"):
        renew_url = ""
    return {
        "level": level,
        "days_left": days_left,
        "expires_at": expires_at,
        "renew_url": renew_url,
    }


def read_tenant_notice(now: Optional[float] = None,
                       path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """读实例数据区的到期提醒；文件缺失/坏/陈旧一律 None（绝不抛）。"""
    p = path if path is not None else NOTICE_FILE
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 无文件=非托管/无提醒，任何读取问题都等价
        return None
    try:
        return parse_tenant_notice(raw, float(time.time() if now is None else now))
    except Exception:  # noqa: BLE001 - 横幅数据绝不拖垮状态接口
        return None
