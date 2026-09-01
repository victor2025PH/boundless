# -*- coding: utf-8 -*-
"""小智「Windows 操控 runner」受控机注册表（实施91 P1-0，2026-08-30）。

安全模型（docs/实施91 §3.3/§4；改前先读，与 pairing.py 同哲学）：
1. **机器是白名单**（"只管自己人"的字面实现）：受控机只能来自配置
   ``assistant.pc_runner.machines`` 声明的静态清单——不做「扫码动态接入
   任意机器」（那是 P3 真客户机 + 跨用户授权才需要的）。LLM/前端只能提名
   清单内的 machine_id，注册表按 id 换出 base_url，**绝不接受任意 URL**
   （与 ui_anchors「LLM 提名 id、服务端换真身」同构）。
2. **runner 侧另有共享 token 自保**：runner 服务只接受携带正确
   ``Authorization: Bearer <token>`` 的内网请求（token 走环境变量 /
   config.local overlay，**不入 git**，不进本注册表、不进日志、不回前端）。
   本模块只管「哪些机器可被操作」，token 的存取在 runner_client 层。
3. **运行时踢下线**：``revoke(mid)`` 让某受控机对小智接口立即不可用
   （进程内，重启回落配置态）；面板「已连接受控机」列表可随时踢。
4. **审计标记**：受控机发起的动作审计行 actor 带 ``@pc:<machine_id>``
   后缀（assistant_action_routes 接线），谁在哪台机做了什么一目了然。

进程内存储 + lock，与 pairing.py / actions.py 同风格。纯逻辑零 HTTP
（HTTP 在 runner_client.py）。门禁 ``tests/test_runner_pairing.py``。
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional

# machine_id 允许字符：字母数字 + 连字符/下划线（对齐 machines.json 的 id
# 命名，如 zhuji/kouxing）。防路径穿越 / 注入 / 审计行污染。
_MID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")

_LOCK = threading.Lock()
# 配置载入的白名单：machine_id -> {"id", "base_url", "label"}（无 token）
_MACHINES: Dict[str, Dict[str, str]] = {}
# 运行时踢下线集（进程内，重启回落配置态）
_REVOKED: set = set()


def valid_machine_id(mid: str) -> bool:
    return bool(_MID_RE.match(str(mid or "")))


def load_machines(entries: Any) -> int:
    """从配置载入受控机白名单（幂等，整表覆盖）。

    entries 形如 ``[{"id": "zhuji", "base_url": "http://127.0.0.1:18760",
    "label": "主机"}]``。非法 id / 缺 base_url 的条目静默跳过（宁缺勿滥）。
    返回载入条数。载入不清空 _REVOKED（运行时踢下线跨重配保留）。
    """
    valid: Dict[str, Dict[str, str]] = {}
    if isinstance(entries, list):
        for e in entries:
            if not isinstance(e, dict):
                continue
            mid = str(e.get("id") or "").strip()
            base = str(e.get("base_url") or "").strip()
            if not valid_machine_id(mid) or not base:
                continue
            # 只接受内网 http(s)；绝不接受非 http scheme（防 file:// 等）
            low = base.lower()
            if not (low.startswith("http://") or low.startswith("https://")):
                continue
            valid[mid] = {"id": mid, "base_url": base.rstrip("/"),
                          "label": str(e.get("label") or mid)[:40]}
    with _LOCK:
        _MACHINES.clear()
        _MACHINES.update(valid)
    return len(valid)


def get_machine(mid: str) -> Optional[Dict[str, str]]:
    """取一台可用受控机 {id, base_url, label}；不在白名单/被踢=None。"""
    key = str(mid or "").strip()
    with _LOCK:
        if key in _REVOKED:
            return None
        ent = _MACHINES.get(key)
        return dict(ent) if ent else None


def machine_ids() -> List[str]:
    """当前可用（在白名单且未被踢）的 machine_id 列表——供规划器提示用。"""
    with _LOCK:
        return [m for m in _MACHINES if m not in _REVOKED]


def list_machines() -> List[Dict[str, Any]]:
    """面板用：全部白名单机 + 踢下线态（**绝不含 token**）。"""
    with _LOCK:
        out = []
        for mid, ent in _MACHINES.items():
            row = dict(ent)
            row["revoked"] = mid in _REVOKED
            out.append(row)
    out.sort(key=lambda r: r["id"])
    return out


def revoke(mid: str) -> bool:
    """踢下线：对小智接口立即不可用。返回是否真的踢了（在白名单内）。"""
    key = str(mid or "").strip()
    with _LOCK:
        if key not in _MACHINES:
            return False
        _REVOKED.add(key)
        return True


def restore(mid: str) -> bool:
    key = str(mid or "").strip()
    with _LOCK:
        if key in _REVOKED:
            _REVOKED.discard(key)
            return True
        return False


def is_revoked(mid: str) -> bool:
    with _LOCK:
        return str(mid or "").strip() in _REVOKED


def actor_suffix(mid: str) -> str:
    """审计 actor 后缀：@pc:<machine_id>（id 已白名单校验，安全拼接）。"""
    key = str(mid or "").strip()
    return f"@pc:{key}" if valid_machine_id(key) else "@pc:?"


def _reset_for_test() -> None:
    with _LOCK:
        _MACHINES.clear()
        _REVOKED.clear()
