"""主链档位「单一口径」摘要（2026-09-17 算力模式表述纠偏沉淀）。

事故：09-17 03:15 ``ai.primary`` 经审计合规切到 ``local``（173 vLLM 主链），但告警文案 /
运维通报 / 官网看板 / GPU 水位卡各自去读 overlay、账本、写死常量——五个出口十二小时里
仍用 08-22「主链锁 cloud、本地只是兜底」的叙事描述系统。根因不是某个出口写错，而是
**没有一个所有出口共用的档位真相**。

本模块就是那个真相：纯函数 ``build_summary(config, effective=..., lock=...)`` 把
``ai.primary`` / ``ai.primary_lock`` / ``ai.base_url`` / ``ai.key_pool`` / ``ai.fallback``
折成一份**零密钥**、可直接渲染的字典（档位、锁、实际降级顺序、各档角色标签、主链一句话、
回落链一句话、云端计费厂商）。所有表达层只消费它：

- 引擎内：``webhook_notifier``（切档通知 / 每日摘要）、``unified_inbox_setup_routes``
  （``GET /api/setup/ai-primary/summary``）；
- 引擎外：``tools/send_ops_group_report.py``、``tools/compute_status_report.py`` 经
  该接口取，接口不可达才回落各自的 overlay 派生（形状与此处同款）。

改档位语义先改这里，再由门禁 ``tests/test_ai_primary_mode_copy_gate.py`` 保证各出口
渲染结果与档位不矛盾。绝不抛：摘要是表达层设施，不能反过来弄崩主链。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_MODES = ("cloud", "local", "local_only")

MODE_LABELS = {
    "cloud": "云端主链",
    "local": "本地主链（可回落云端）",
    "local_only": "本地主链（严格隐私，不回落）",
}

# 官网推送器慢车道（overlay / summary）默认 60s。切档后写这个文件，循环下一拍
# 立刻重读——``--once`` 会撞 18797 单例锁秒退，杀进程又太重。路径与
# ``tools/compute_status_report.py`` 的 ``NUDGE_FILE`` 必须一致。
BOARD_NUDGE_PATH = Path(r"D:\chengjie-instances\.ops\compute_pusher.nudge")


def nudge_compute_board() -> bool:
    """通知官网推送器下一圈重读档位（best-effort，失败返回 False，绝不抛）。"""
    try:
        BOARD_NUDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BOARD_NUDGE_PATH.write_text(str(time.time()), encoding="utf-8")
        return True
    except Exception:
        return False


def vendor_of(base_url: Any) -> str:
    """端点 URL → 厂商标签。未知厂商回主机名，绝不猜。"""
    u = str(base_url or "").strip()
    if not u:
        return ""
    if "siliconflow" in u:
        return "siliconflow"
    if "deepseek" in u:
        return "deepseek-official"
    m = re.search(r"192\.168\.\d+\.(\d+)", u)
    if m:
        return f"LAN .{m.group(1)}"
    m = re.match(r"https?://([^/]+)", u)
    return m.group(1) if m else u


def chain_roles(mode: str, lock: str = "") -> Dict[str, Any]:
    """按档位给三档排序并贴角色标签（纯函数，看板标题直接用）。

    local/local_only：① 本地主链 → ② 云端回落（local_only 不回落）→ ③ 云 key 池；
    cloud：① 云主链 → ② 云 key 池 → ③ 本地兜底。
    """
    mode = str(mode or "cloud").strip().lower()
    if mode not in _MODES:
        mode = "cloud"
    lock = str(lock or "").strip().lower()
    if mode in ("local", "local_only"):
        order = ["local", "cloud", "pool"]
        roles = {
            "local": "① 主链 · 本地 vLLM",
            "cloud": ("② 不回落（隐私档，本地失败落 canned）" if mode == "local_only"
                      else "② 回落 · 云端"),
            "pool": "③ 云端备用 key",
        }
    else:
        order = ["cloud", "pool", "local"]
        roles = {"cloud": "① 主链 · 云端", "pool": "② 备用 key · 云端",
                 "local": "③ 兜底 · 本地 vLLM"}
    return {"mode": mode, "lock": lock or None, "order": order, "roles": roles,
            "mode_label": MODE_LABELS.get(mode, mode)}


def _pool_entries(ai: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in ((ai.get("key_pool") or {}).get("keys") or []):
        if not isinstance(item, dict) or not str(item.get("api_key") or "").strip():
            continue
        base = str(item.get("base_url") or ai.get("base_url") or "")
        out.append({"vendor": vendor_of(base), "base_url": base,
                    "model": str(item.get("model") or ai.get("model") or "")})
    return out


def build_summary(config: Dict[str, Any], *, effective: Optional[str] = None,
                  lock: Optional[str] = None) -> Dict[str, Any]:
    """把配置折成可渲染的档位摘要（零密钥）。

    ``effective``＝运行时 AIClient 实际档位（调用方有 ai_client 就传，没有按配置）；
    ``lock``＝已解析锁值（不传则按 ``ai.primary_lock`` 自行归一化）。
    """
    try:
        ai = (config or {}).get("ai") or {}
        if not isinstance(ai, dict):
            ai = {}
        configured = str(ai.get("primary") or "cloud").strip().lower()
        if configured not in _MODES:
            configured = "cloud"
        eff = str(effective or configured).strip().lower()
        if eff not in _MODES:
            eff = configured
        if lock is None:
            try:
                from src.ai.ai_primary_audit import resolve_lock
                lock = resolve_lock(ai)
            except Exception:
                lock = ""
        lock = str(lock or "").strip().lower()
        fb = ai.get("fallback") if isinstance(ai.get("fallback"), dict) else {}
        local_base = str(fb.get("base_url") or "").strip().rstrip("/")
        local_model = str(fb.get("model") or "").strip()
        local_enabled = bool(fb.get("enabled", False))
        local_ready = bool(local_enabled and local_base and local_model)
        cloud_base = str(ai.get("base_url") or "").strip()
        cloud = {"vendor": vendor_of(cloud_base), "base_url": cloud_base,
                 "model": str(ai.get("model") or "").strip()}
        pool = _pool_entries(ai)
        roles = chain_roles(eff, lock)
        billing = str(((ai.get("cost_guard") or {}).get("provider")) or "").strip() \
            if isinstance(ai.get("cost_guard"), dict) else ""
        billing = billing or cloud["vendor"]
        lock_txt = f"锁 {lock}" if lock else "未设锁"
        if eff in ("local", "local_only"):
            primary_text = (f"本地 vLLM {local_model or '本地模型'}（档位 {eff}，{lock_txt}）")
            steps = [f"本地 {vendor_of(local_base)} vLLM {local_model}".rstrip()]
            if eff == "local_only":
                steps.append("本地失败不回落云端（隐私档）→ canned")
            else:
                steps.append(f"回落 {cloud['vendor']} {cloud['model']}".rstrip())
                for p in pool:
                    steps.append(f"key 池 {p['vendor']}")
                steps.append("canned")
        else:
            primary_text = f"云端 {cloud['vendor']} {cloud['model']}（档位 cloud，{lock_txt}）".replace("  ", " ")
            steps = [f"云端 {cloud['vendor']} {cloud['model']}".rstrip()]
            for p in pool:
                steps.append(f"key 池 {p['vendor']}")
            if local_ready:
                steps.append(f"本地兜底 {vendor_of(local_base)} vLLM {local_model}".rstrip())
            steps.append("canned")
        return {
            "ok": True,
            "mode": configured,
            "effective": eff,
            "divergent": configured != eff,
            "lock": lock or None,
            "locked": bool(lock),
            "mode_label": roles["mode_label"],
            "order": roles["order"],
            "roles": roles["roles"],
            "local": {"base_url": local_base, "model": local_model or None,
                      "enabled": local_enabled, "ready": local_ready,
                      "vendor": vendor_of(local_base)},
            "cloud": cloud,
            "pool": pool,
            "billing_provider": billing or None,
            "primary_text": primary_text,
            "chain_text": " → ".join(steps),
        }
    except Exception:
        # 摘要坏了不许连累主链：给一份最小可渲染形状
        return {"ok": False, "mode": "cloud", "effective": "cloud", "lock": None, "locked": False,
                "mode_label": MODE_LABELS["cloud"], "order": ["cloud", "pool", "local"],
                "roles": chain_roles("cloud")["roles"], "local": {}, "cloud": {}, "pool": [],
                "billing_provider": None, "primary_text": "（档位摘要不可用）", "chain_text": ""}


__all__ = ["build_summary", "chain_roles", "vendor_of", "MODE_LABELS",
           "nudge_compute_board", "BOARD_NUDGE_PATH"]
