# -*- coding: utf-8 -*-
"""LINE Letter-Sealing 密钥自愈（A2，2026-09-03；证据钧机 3U298U 20:46-20:47 窗）。

事故机制（两个已修件的交叉盲区）
--------------------------------
``kick_stuck_send``（2026-09-02 WEXX7E 的恢复锤）在签名桥 Node 进程僵死时直接
``terminate()`` 它，让悬死线程的 ``readline()`` 见 EOF、各级锁释放——这一半是对的，
下一笔请求 okline 的 ``_ensure_started`` 会自动重启桥。**但 E2EE 密钥不在 Python 里**：

    okline 的 ``E2EEManager`` 只存 ``my_keys: {keyId -> wasm handle}``，handle 是**那个
    Node 进程里 ltsm.wasm 的句柄整数**。桥一换进程，句柄全成野指针，而
    ``is_ready()`` 只看 ``my_keys`` 非空 → **仍然返回 True**。

于是重启后的第一笔 Letter-Sealed 发送走进 okline 的「code 82 → 自动加封重发」分支，
拿野句柄去 ``e2ee_encrypt_v2``，结果是密钥类报错（网关侧表现为
``Item_e2ee_key_not_exists`` 一族）——而且**没有任何一层会去重建密钥**，同一个号此后
每一笔加封发送都失败，直到 worker 整体重启。3U298U 20:46:28 踢桥、20:46:47 起
发送异常正是这个窗。

修法（两条，都在本模块，供 ``LineProtocolWorker`` 调用）
----------------------------------------------------
1. **懒重建**：踢桥即打脏标（``mark_bridge_restarted``），下一次发送前
   ``ensure_e2ee_ready`` 从 session 文件（``save_tokens`` 写的 ``e2ee.keys``
   导出块）经 ``load_from_export`` 把密钥重新装进**新桥**，并丢弃引用了旧句柄的
   信道缓存。零网络、零扫码。
2. **遇错自愈重试一次**：发送抛出密钥类错误（``is_e2ee_key_error``）时强制重握手
   （丢信道 + 重装密钥）后**重试一次**。只重试一次是刻意的——真的是对端关了
   Letter Sealing / 账号被降级时，无限重试只会把节流打满。

设计：纯函数 + 只读 session 文件，全部 best-effort 绝不抛（自愈链自己抛异常会把
本来只是「这条没发出去」放大成「worker 挂了」）。okline 私有属性访问与
``kick_stuck_send`` 同一取舍：属性缺失（版本漂移）时安静降级。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "E2EE_KEY_ERROR_MARKERS",
    "is_e2ee_key_error",
    "read_session_e2ee",
    "drop_e2ee_channels",
    "rebuild_e2ee_from_session",
]

#: 网关/库侧「密钥不存在或对不上」的错误特征（小写子串匹配，顺序无关）。
#: ``item_e2ee_key_not_exists``＝钧机实锤的 LINE 服务端 reason；其余是同族形态与
#: okline 自己在密钥缺失时抛的 RuntimeError 文案（见 ``e2ee.py`` 的几处 raise）。
E2EE_KEY_ERROR_MARKERS = (
    "item_e2ee_key_not_exists",
    "e2ee_key_not_exists",
    "no local e2ee key",
    "e2ee not initialised",
    "could not negotiate e2ee key",
    "no e2ee public key",
)


def _error_text(exc: Any) -> str:
    """把异常摊平成可匹配的小写文本：str + reason + metadata（Thrift 错误细节在后两处）。"""
    bits = []
    try:
        bits.append(str(exc))
    except Exception:
        pass
    for attr in ("reason", "metadata", "raw"):
        try:
            v = getattr(exc, attr, None)
            if v:
                bits.append(v if isinstance(v, str) else json.dumps(
                    v, ensure_ascii=False, default=str))
        except Exception:
            continue
    return " ".join(bits).lower()


def is_e2ee_key_error(exc: Any) -> bool:
    """该异常是否属「E2EE 密钥不存在/对不上」——即**重握手有可能救回来**的那一类。

    刻意不认 okline 的 ``E2EE_SENDER_DISABLED`` / ``E2EE_RECEIVER_DISABLED``
    （对端真关了 Letter Sealing）：那种重握手一万次也没用，属配置事实不是故障。
    """
    if exc is None:
        return False
    text = _error_text(exc)
    if not text:
        return False
    return any(m in text for m in E2EE_KEY_ERROR_MARKERS)


def read_session_e2ee(tokens_path: str) -> Dict[str, Any]:
    """从 session 文件读回 ``e2ee`` 导出块（``OkLine.save_tokens`` 写的那份）。

    返回 ``{}`` 表示「这个号没有可重建的密钥」——从没扫码登录过 E2EE、或旧版
    session 文件没有该段。读文件失败同样返回 ``{}``（调用方按「无法重建」处理）。
    """
    path = str(tokens_path or "").strip()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        logger.debug("[line_e2ee] session 文件读取失败 path=%s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    blob = data.get("e2ee")
    if not isinstance(blob, dict):
        return {}
    keys = blob.get("keys")
    if not isinstance(keys, dict) or not keys:
        return {}
    return blob


def drop_e2ee_channels(client: Any) -> int:
    """丢弃引用了**旧桥句柄**的信道/群密钥缓存，返回丢弃条数。

    ``_peer_channels`` / ``_group_keys`` 里存的都是 wasm 句柄整数，桥换进程后
    全是野值——不清掉的话 ``_channel_for_send`` 会命中缓存、压根不会重新协商。
    """
    dropped = 0
    mgr = getattr(client, "e2ee", None)
    if mgr is None:
        return 0
    for attr in ("_peer_channels", "_group_keys"):
        try:
            cache = getattr(mgr, attr, None)
            if isinstance(cache, dict) and cache:
                dropped += len(cache)
                cache.clear()
        except Exception:
            logger.debug("[line_e2ee] 清 %s 失败", attr, exc_info=True)
    return dropped


def rebuild_e2ee_from_session(
    client: Any, tokens_path: str, *, account_id: str = "",
) -> Dict[str, Any]:
    """把密钥重新装进**当前（可能是刚重启的）签名桥**。

    返回 ``{"ok": bool, "keys": int, "dropped": int, "reason": str}``：

    - ``ok=True``  → ``e2ee.is_ready()`` 为真且句柄来自当前桥，加封发送可以继续；
    - ``reason="no_session_keys"`` → 该号没有可重建的密钥（没扫码登录过 E2EE）；
      这**不是**故障——纯文本会话不需要 Letter Sealing，调用方照常发送即可；
    - ``reason="no_manager"`` / ``"load_failed"`` / ``"exception:*"`` → 重建没成，
      调用方照常尝试发送（让真实错误自己浮出来），别在这里替它判死。
    """
    out: Dict[str, Any] = {"ok": False, "keys": 0, "dropped": 0, "reason": ""}
    mgr = getattr(client, "e2ee", None)
    if mgr is None or not hasattr(mgr, "load_from_export"):
        out["reason"] = "no_manager"
        return out
    blob = read_session_e2ee(tokens_path)
    if not blob:
        out["reason"] = "no_session_keys"
        return out
    # 顺序要紧：先清信道（它们引用旧句柄），再装密钥（装的是新桥的句柄）
    out["dropped"] = drop_e2ee_channels(client)
    try:
        ok = bool(mgr.load_from_export(blob))
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"exception:{type(exc).__name__}"
        logger.warning(
            "[line_e2ee] 密钥重建抛错 acct=%s: %s（本笔照常尝试发送）",
            account_id or "-", str(exc)[:160])
        return out
    try:
        out["keys"] = len(getattr(mgr, "my_keys", {}) or {})
    except Exception:
        out["keys"] = 0
    out["ok"] = ok
    if not ok:
        out["reason"] = "load_failed"
        logger.warning(
            "[line_e2ee] 密钥重建未就绪 acct=%s keys=%s（本笔照常尝试发送）",
            account_id or "-", out["keys"])
    else:
        logger.warning(
            "[line_e2ee] 签名桥重启后已重建 E2EE 密钥 acct=%s keys=%s 丢弃旧信道=%s"
            "（此前这里会拿野句柄加封 → 该号此后每笔加封发送都失败直到重启 worker）",
            account_id or "-", out["keys"], out["dropped"])
    return out
