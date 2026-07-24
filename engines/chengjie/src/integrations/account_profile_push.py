"""账号「官方资料修改」推送（accounts.profile_push，写方向 P1）。

与 ``account_self_profile``（读方向：登录后富集 self_* 进 registry meta）互补：
本模块把后台编辑的 **昵称 / 签名(bio·status) / 头像** 推送到平台官方——

- **Telegram**：pyrogram 协议直改（``update_profile`` / ``set_profile_photo``）；
- **WhatsApp**：经 Baileys Node 微服务 ``POST /accounts/{id}/profile``；
- **LINE / Messenger**：官方不提供可用写口 → 声明为 ``manual``（后台只读展示，
  提示到手机官方 App 修改）。

设计约束（对齐仓库纪律）：
- **纯函数可单测**：flag 读取 / 平台能力表 / 字段校验 / 冷却计算 / 头像加工 /
  meta 合并全部零副作用；副作用（HTTP / pyrogram RPC）集中在 ``push_*`` 执行器，
  且网络封装可注入（测试零真号）。
- **read-merge-write**：``merge_push_meta`` 只返回合并后的新 dict（不改入参）；
  调用方必须先读旧 meta 再写回——``registry.upsert(meta=)`` 是整块覆盖语义，
  直写会清掉 ``session_string`` 等敏感键（仓库血泪教训）。
- **冷却护栏**：平台会风控频繁资料变更（尤其新号）→ 每账号 ``cooldown_hours``
  最小间隔，由 ``cooldown_remaining`` 判定（meta.profile_push_last_ts）。
- **跨 loop 安全**：路由跑在 web loop；编排器受管 worker 的 client 也活在 web
  loop，但进程主 default client 活在主 Telegram loop → 所有 pyrogram 调用过
  ``run_on_client_loop``（同 loop 直 await / 异 loop run_coroutine_threadsafe）。
- 执行器抛错一律用 **i18n 键**作 args[0]（``err.acct.profile_*``），路由层
  ``tr()`` 出双语文案（响应文案禁止硬编码中文，CJK ratchet 门禁）。
- **人设一键填充**（use_persona）素材定位与**推送可观测计数**同居本模块：
  名称取 PersonaManager profile、头像取人设锁脸基准照（``find_persona_face_ref``
  复刻 persona_media_routes ``_find_face_ref`` 的磁盘定位为纯函数）；计数进程内
  best-effort（风格对齐 ``account_self_profile._STATS``），经
  ``/api/workspace/metrics.profile_push`` 与 Prometheus ``profile_push_total`` 观测。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 头像统一裁方压缩的目标边长 / JPEG 质量（各平台官方头像都是方图）
_AVATAR_SIDE = 640
_AVATAR_JPEG_QUALITY = 88
# 审计日志（meta.profile_push_log）保留条数
_PUSH_LOG_KEEP = 10

# ── 平台能力表 ────────────────────────────────────────────────────────────────
# mode=direct：后台可直改；mode=manual：平台无可用写口，只能手机官方 App 改。
# status 语义：Telegram=bio；WhatsApp=状态签名（About）。
PLATFORM_CAPS: Dict[str, Dict[str, Any]] = {
    "telegram": {"mode": "direct", "name": True, "status": True, "avatar": True},
    "whatsapp": {"mode": "direct", "name": True, "status": True, "avatar": True},
    "line": {"mode": "manual", "name": False, "status": False, "avatar": False},
    "messenger": {"mode": "manual", "name": False, "status": False, "avatar": False},
}

# 平台官方字段长度上限（超限在推送前拦下，不白打 API）
_FIELD_LIMITS: Dict[str, Dict[str, int]] = {
    "telegram": {"name": 64, "status": 70},    # first_name 64 / bio 70
    "whatsapp": {"name": 25, "status": 139},   # pushname 25 / About 139
}


# ── feature flags（accounts.profile_push.*） ─────────────────────────────────

def _pp_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return ((config or {}).get("accounts", {}) or {}).get("profile_push", {}) or {}


def profile_push_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """总开关，默认关（遵仓库「新子系统默认 false」约定）。"""
    return bool(_pp_cfg(config).get("enabled", False))


def push_cooldown_hours(config: Optional[Dict[str, Any]]) -> float:
    """同一账号两次修改的最小间隔（小时），默认 24。非法值回落默认。"""
    try:
        return float(_pp_cfg(config).get("cooldown_hours", 24))
    except (TypeError, ValueError):
        return 24.0


def max_avatar_mb(config: Optional[Dict[str, Any]]) -> float:
    """头像原图体积上限（MB），默认 5。非法值回落默认。"""
    try:
        return float(_pp_cfg(config).get("max_avatar_mb", 5))
    except (TypeError, ValueError):
        return 5.0


def fresh_account_days(config: Optional[Dict[str, Any]]) -> int:
    """新接入账号视为「新号」的天数（accounts.profile_push.fresh_account_days，默认 7）。

    新号改资料是平台风控高危动作 → 详情页据此在保存前二次确认。非法值回落默认。
    """
    try:
        return int(float(_pp_cfg(config).get("fresh_account_days", 7)))
    except (TypeError, ValueError):
        return 7


# ── 推送可观测计数（进程内 best-effort，风格对齐 account_self_profile._STATS）──

_STAT_KEYS = ("attempts", "success", "partial", "failed",
              "cooldown_blocked", "offline_blocked", "persona_fill",
              "align_runs")
# 批量对齐单次上限（防运营误点拖垮平台风控窗）；串行间隔见路由层
ALIGN_MAX_ACCOUNTS = 20
_BY_PLATFORM_KEYS = ("attempts", "success")
_BY_PLATFORM_MAX = 16          # 平台是闭集（telegram/whatsapp），上限纯防御
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {
    **{k: 0 for k in _STAT_KEYS},
    "by_platform": {},          # platform -> {"attempts": n, "success": n}
    "last_ts": 0.0,
}


def _san_platform(platform: Any) -> str:
    return re.sub(r"[^a-z0-9_\-\.]", "", str(platform or "").lower())[:24]


def bump_push_stat(key: str, platform: Optional[str] = None, n: int = 1) -> None:
    """推送计数 +n（未知 key 忽略）；``by_platform`` 只跟踪 attempts/success。"""
    k = str(key or "")
    with _STATS_LOCK:
        if k in _STAT_KEYS:
            _STATS[k] = int(_STATS.get(k, 0)) + int(n)
        _STATS["last_ts"] = time.time()
        if platform and k in _BY_PLATFORM_KEYS:
            plat = _san_platform(platform)
            bp = _STATS["by_platform"]
            if not plat or (plat not in bp and len(bp) >= _BY_PLATFORM_MAX):
                return
            slot = bp.setdefault(plat, {"attempts": 0, "success": 0})
            slot[k] = int(slot.get(k, 0)) + int(n)


def record_push_result(platform: str, ok: bool, partial: bool = False) -> None:
    """按结果计数：ok=至少一字段成功（与路由 200 同口径）；partial 仅在 ok 下单独可见。"""
    if ok:
        bump_push_stat("success", platform)
        if partial:
            bump_push_stat("partial")
    else:
        bump_push_stat("failed")


def get_profile_push_stats() -> Dict[str, Any]:
    """计数快照（深拷贝，含 by_platform）；``active``=有过尝试（供 ops 卡零流量隐藏）。"""
    with _STATS_LOCK:
        out = copy.deepcopy(_STATS)
    out["active"] = int(out.get("attempts") or 0) > 0
    return out


def reset_profile_push_stats() -> None:
    with _STATS_LOCK:
        for k in _STAT_KEYS:
            _STATS[k] = 0
        _STATS["by_platform"] = {}
        _STATS["last_ts"] = 0.0


def _prom_esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def dump_profile_push_prom() -> str:
    """Prometheus exposition：profile_push_total{op=} + profile_push_platform_total。"""
    st = get_profile_push_stats()
    lines = [
        "# HELP profile_push_total Account official profile push operations "
        "(process lifetime)",
        "# TYPE profile_push_total counter",
    ]
    for k in _STAT_KEYS:
        lines.append(f'profile_push_total{{op="{k}"}} {int(st.get(k) or 0)}')
    lines += [
        "# HELP profile_push_platform_total Account official profile push "
        "operations by platform",
        "# TYPE profile_push_platform_total counter",
    ]
    for plat, ops in sorted((st.get("by_platform") or {}).items()):
        for op in _BY_PLATFORM_KEYS:
            lines.append(
                f'profile_push_platform_total{{platform="{_prom_esc(plat)}"'
                f',op="{op}"}} {int((ops or {}).get(op) or 0)}')
    return "\n".join(lines) + "\n"


# ── 人设一键填充素材（use_persona） ──────────────────────────────────────────

# 与 persona_media_routes 同口径的锁脸基准照约定（复刻为纯函数，勿反向依赖路由闭包）
_FACE_REF_STEM = "face_ref"
_FACE_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _safe_pid(pid: Any) -> str:
    """人设 id 收敛为安全目录名（防路径穿越；与 persona_media_routes._safe_pid 同规则）。"""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(pid or ""))[:64]
    return s or "default"


def persona_face_album_dir(pid: str, config: Optional[Dict[str, Any]]) -> Path:
    """锁脸相册目录：companion.selfie.provider.album_dir/<pid>。

    相对路径以 cwd 为基准（主程序 cwd=仓库根，与 persona_media_routes
    ``_face_album_dir`` 的回落分支同口径）。
    """
    rel = "assets/persona_media"
    try:
        rel = str(((((config or {}).get("companion") or {}).get("selfie") or {})
                   .get("provider") or {}).get("album_dir") or rel)
    except Exception:  # noqa: BLE001 — config 形状异常回落默认目录
        pass
    base = Path(rel)
    if not base.is_absolute():
        base = Path.cwd() / base
    return base / _safe_pid(pid)


def find_persona_face_ref(
    pid: str, config: Optional[Dict[str, Any]],
) -> Optional[Path]:
    """人设锁脸基准照（``face_ref.<img>``）的磁盘定位；找不到/不可读 → None。"""
    d = persona_face_album_dir(pid, config)
    try:
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file() and p.stem.lower() == _FACE_REF_STEM \
                        and p.suffix.lower() in _FACE_IMAGE_EXT:
                    return p
    except Exception:  # noqa: BLE001
        pass
    return None


def resolve_persona_fill(
    pid: str, config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """人设一键填充素材：``{"id", "name", "face_ref_path"}``；pid 空/查无 → ``{}``。

    name 取 PersonaManager profile（profiles_runtime.yaml 的 ``name`` 字段）；
    face_ref_path 为锁脸基准照绝对路径字符串（无则 ""）。PersonaManager 不可用
    /任何异常 → ``{}``（best-effort，绝不阻塞资料详情/推送主流程）。
    """
    pid = str(pid or "").strip()
    if not pid:
        return {}
    try:
        from src.utils.persona_manager import PersonaManager
        prof = PersonaManager.get_instance().get_persona_by_id(pid)
    except Exception:  # noqa: BLE001
        return {}
    if not prof:
        return {}
    face = find_persona_face_ref(pid, config)
    return {
        "id": pid,
        "name": str(prof.get("name") or "").strip(),
        "face_ref_path": str(face) if face is not None else "",
    }


def meta_persona_id(meta: Optional[Dict[str, Any]]) -> str:
    """从 registry meta 取绑定人设 id（``persona_id`` 优先，回落 ``persona_ids[0]``）。"""
    m = meta or {}
    pid = str(m.get("persona_id") or "").strip()
    if pid:
        return pid
    pids = m.get("persona_ids") or []
    if isinstance(pids, (list, tuple)) and pids:
        return str(pids[0] or "").strip()
    return ""


def accounts_bound_to_persona(
    rows: List[Dict[str, Any]], persona_id: str,
) -> List[Dict[str, Any]]:
    """从 registry ``list()`` 结果筛出绑定到 ``persona_id`` 的账号（保输入序）。"""
    want = str(persona_id or "").strip()
    if not want:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if meta_persona_id((row or {}).get("meta")) == want:
            out.append(row)
    return out


def profile_churn_count(
    meta: Optional[Dict[str, Any]],
    now: Optional[float] = None,
    *,
    days: float = 7.0,
) -> int:
    """近 ``days`` 天内成功资料推送次数（读 ``profile_push_log``，仅 ``ok=True``）。

    供详情页「改名频繁」提示与 fleet-health 钩子；坏条目静默跳过。
    """
    cutoff = float(now if now is not None else time.time()) - float(days) * 86400.0
    n = 0
    for e in list((meta or {}).get("profile_push_log") or []):
        if not isinstance(e, dict) or not e.get("ok"):
            continue
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            n += 1
    return n


def predict_align_status(
    platform: str,
    meta: Optional[Dict[str, Any]],
    *,
    now: float,
    cooldown_hours: float,
    fresh_days: int,
    created_at: float,
    online: bool,
    target_name: str = "",
    has_avatar: bool = False,
    current_name: str = "",
) -> str:
    """批量对齐 dry-run 预判：``would_push`` / ``skipped_*``（不含真正推送失败）。

    批量路径对新号默认 skip（``skipped_fresh``）——单账号保存仍只是二次确认，
    批量误点的风控面更大，宁漏勿滥。

    额外优化：无头像可推且当前昵称已等于人设名 → ``skipped_aligned``（不白打 API）。
    """
    if capabilities(platform).get("mode") == "manual":
        return "skipped_manual"
    if cooldown_remaining(meta, now, cooldown_hours) > 0:
        return "skipped_cooldown"
    try:
        age_ok = float(created_at) > 0
        age_days = ((float(now) - float(created_at)) / 86400.0) if age_ok else None
    except (TypeError, ValueError):
        age_days = None
    if age_days is not None and age_days < float(fresh_days):
        return "skipped_fresh"
    if not online:
        return "skipped_offline"
    tn = str(target_name or "").strip()
    cn = str(current_name or "").strip()
    if (not has_avatar) and tn and cn and tn == cn:
        return "skipped_aligned"
    return "would_push"


def same_platform_align_risk(
    results: List[Dict[str, Any]],
    *,
    push_statuses: Tuple[str, ...] = ("would_push", "pushed"),
) -> List[Dict[str, Any]]:
    """同平台 ≥2 账号将/已对齐到同一人设名 → 关联封号风险清单。

    返回 ``[{"platform", "count"}, ...]``（按平台名排序）；无风险 → ``[]``。
    """
    counts: Dict[str, int] = {}
    for r in results or []:
        if str((r or {}).get("status") or "") not in push_statuses:
            continue
        plat = str((r or {}).get("platform") or "").lower().strip()
        if not plat:
            continue
        counts[plat] = int(counts.get(plat) or 0) + 1
    return [{"platform": p, "count": n}
            for p, n in sorted(counts.items()) if n >= 2]


def parse_align_include(raw: Any) -> Optional[set]:
    """解析批量对齐 ``include`` 白名单。

    - ``None`` / 缺省 → 不过滤（``None``）；
    - 列表项 ``"platform:account_id"``（或 ``{platform, account_id}``）→
      ``{(plat, aid), ...}``；空列表 = 显式排除全部。
    非法项静默跳过。
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    out = set()
    for item in raw:
        plat, aid = "", ""
        if isinstance(item, str):
            s = item.strip()
            if ":" not in s:
                continue
            plat, aid = s.split(":", 1)
        elif isinstance(item, dict):
            plat = str(item.get("platform") or "")
            aid = str(item.get("account_id") or "")
        else:
            continue
        plat = plat.lower().strip()
        aid = aid.strip()
        if plat and aid:
            out.add((plat, aid))
    return out


def account_align_key(platform: str, account_id: str) -> str:
    """稳定账号键：``platform:account_id``（与前端勾选/include 同口径）。"""
    return f"{str(platform or '').lower().strip()}:{str(account_id or '').strip()}"


_ALIGN_FIELDS = ("name", "avatar")


def parse_align_fields(raw: Any) -> Optional[set]:
    """解析批量对齐 ``fields`` 白名单（字段级对齐：只推昵称 / 只推头像）。

    - ``None`` / 缺省 / 空 / 非列表 → ``None``（= 推全部可推字段，向后兼容）；
    - 列表 → 取 ``{"name","avatar"}`` 交集；过滤后为空视作不限制（``None``）。
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    out = {str(x or "").lower().strip() for x in raw}
    out &= set(_ALIGN_FIELDS)
    return out or None


# ── 纯函数 ────────────────────────────────────────────────────────────────────

def capabilities(platform: str) -> Dict[str, Any]:
    """该平台的资料修改能力（未知平台按 manual 全 False，宁保守不误开）。"""
    caps = PLATFORM_CAPS.get(str(platform or "").lower())
    if caps is None:
        return {"mode": "manual", "name": False, "status": False, "avatar": False}
    return dict(caps)


def validate_fields(
    platform: str, name: str, status_text: str,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """字段长度校验：超限返回 (错误 i18n 键, format 参数)，合规返回 None。

    name/status 均可选（空串跳过校验）；「至少一个字段」由路由层判（含 avatar）。
    """
    lim = _FIELD_LIMITS.get(str(platform or "").lower())
    if lim is None:
        return None
    if name and len(name) > lim["name"]:
        return ("err.acct.profile_name_too_long", {"max": lim["name"]})
    if status_text and len(status_text) > lim["status"]:
        return ("err.acct.profile_status_too_long", {"max": lim["status"]})
    return None


def cooldown_remaining(
    meta: Optional[Dict[str, Any]], now: float, hours: float,
) -> float:
    """冷却剩余秒数（≤0 表示可改）。读 ``meta.profile_push_last_ts``，无/坏值=可改。"""
    try:
        last = float((meta or {}).get("profile_push_last_ts") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last <= 0 or hours <= 0:
        return 0.0
    return (last + float(hours) * 3600.0) - float(now)


def format_remaining(seconds: float) -> str:
    """冷却剩余的人类可读表达（如 "3.5h"，一位小数、下限 0.1h）——供 429 文案 {remain}。"""
    h = max(0.1, round(max(0.0, float(seconds)) / 3600.0, 1))
    return f"{h}h"


def prepare_avatar(data: bytes, max_mb: float) -> bytes:
    """头像校验 + 规格化：居中裁方 → 640×640 → RGB JPEG(quality=88) bytes。

    - 超 ``max_mb`` → ``ValueError("err.acct.profile_avatar_too_large")``；
    - 打不开/非图/空/缺 PIL → ``ValueError("err.acct.profile_avatar_invalid")``。
    ValueError.args[0] 即 i18n 键，路由层 tr() 出双语文案。
    """
    if not data:
        raise ValueError("err.acct.profile_avatar_invalid")
    if len(data) > float(max_mb) * 1024 * 1024:
        raise ValueError("err.acct.profile_avatar_too_large")
    try:
        import io
        from PIL import Image
    except Exception:  # noqa: BLE001 — 缺 PIL 按无效图处理（仓库有 PIL，纯防御）
        raise ValueError("err.acct.profile_avatar_invalid")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        # 手机拍摄图常带 EXIF Orientation（存储为横置+旋转标记）——不转正会得到
        # 侧躺头像；exif_transpose 无标记时原样返回，best-effort 不阻塞。
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img) or img
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        raise ValueError("err.acct.profile_avatar_invalid")
    w, h = img.size
    if w < 1 or h < 1:
        raise ValueError("err.acct.profile_avatar_invalid")
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    # 显式 LANCZOS：PIL resize 缺省重采样是 NEAREST，缩图会满是锯齿（头像质量事故）
    try:
        _resample = Image.Resampling.LANCZOS  # Pillow ≥ 9.1
    except AttributeError:  # pragma: no cover - 老版 Pillow
        _resample = Image.LANCZOS
    img = img.resize((_AVATAR_SIDE, _AVATAR_SIDE), _resample)
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=_AVATAR_JPEG_QUALITY)
    return out.getvalue()


def merge_push_meta(
    existing_meta: Optional[Dict[str, Any]], fields: List[str], ts: float, ok: bool,
) -> Dict[str, Any]:
    """把本次推送结果叠加进 meta（**不改入参**，返回新 dict——read-merge-write 纪律）。

    - ``ok=True`` 才推 ``profile_push_last_ts``（失败不烧冷却窗，允许立刻重试）；
    - 追加审计条目 ``profile_push_log``: {ts, fields, ok}，保留最近 10 条。
    """
    merged: Dict[str, Any] = dict(existing_meta or {})
    if ok:
        merged["profile_push_last_ts"] = float(ts)
    log = list(merged.get("profile_push_log") or [])
    log.append({"ts": float(ts), "fields": [str(f) for f in (fields or [])],
                "ok": bool(ok)})
    merged["profile_push_log"] = log[-_PUSH_LOG_KEEP:]
    return merged


# ── 跨 loop 安全 helper ───────────────────────────────────────────────────────

def _client_loop(client: Any) -> Any:
    """取 client（或其 ``.client`` 内层）所属的事件 loop；取不到 → None。"""
    loop = getattr(client, "loop", None)
    if loop is None:
        loop = getattr(getattr(client, "client", None), "loop", None)
    return loop


async def run_on_client_loop(client: Any, coro_factory) -> Any:
    """在 client 自己的 loop 上执行 ``coro_factory()``（跨 loop 安全的统一入口）。

    - client 的 loop 与当前 loop 相同 / 取不到 / 未运行 → 直接 await（编排器受管
      worker 的 client 与 web 路由同 loop，走这条）；
    - 不同且在运行 → ``run_coroutine_threadsafe`` 调度过去再 wrap 回本 loop await
      （进程主 default client 活在主 Telegram loop，走这条）。
    """
    target = _client_loop(client)
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if target is None or target is current or not target.is_running():
        return await coro_factory()
    return await asyncio.wrap_future(
        asyncio.run_coroutine_threadsafe(coro_factory(), target))


# ── 副作用执行器（防御式；抛错 args[0]=i18n 键） ─────────────────────────────

async def push_whatsapp(
    cfg: Dict[str, Any],
    account_id: str,
    *,
    name: str = "",
    status_text: str = "",
    avatar_bytes: Optional[bytes] = None,
    post_json=None,
) -> Dict[str, Any]:
    """经 Baileys 微服务改 WhatsApp 官方资料。

    ``POST {base}/accounts/{account_id}/profile``，body 只带非空字段
    ``{name, status_text, avatar_b64}``；Node 约定响应
    ``{ok, applied:{name,status,avatar}, errors:{...}, pushname, avatar_url}``。

    异常语义（RuntimeError.args[0] 为 i18n 键，路由映射状态码）：
    - 账号不在(404) / 未连接(409 或 body error=not_connected) →
      ``err.acct.profile_offline``；
    - 其余网络/服务错误 → ``err.acct.profile_push_failed``。
    ``post_json`` 可注入（默认 ``whatsapp_baileys_login._post_json``，
    其对非 2xx ``raise_for_status`` → 从异常的 response.status_code 判语义）。
    """
    if post_json is None:
        from src.integrations.whatsapp_baileys_login import _post_json as post_json
    from src.integrations.whatsapp_baileys_login import service_base_url
    body: Dict[str, Any] = {}
    if name:
        body["name"] = str(name)
    if status_text:
        body["status_text"] = str(status_text)
    if avatar_bytes:
        body["avatar_b64"] = base64.b64encode(avatar_bytes).decode("ascii")
    url = f"{service_base_url(cfg)}/accounts/{account_id}/profile"
    try:
        res = await post_json(url, body)
    except Exception as ex:  # noqa: BLE001
        status_code = getattr(getattr(ex, "response", None), "status_code", None)
        logger.debug("[profile_push] whatsapp 推送失败 acct=%s status=%s",
                     account_id, status_code, exc_info=True)
        if status_code in (404, 409):
            raise RuntimeError("err.acct.profile_offline") from ex
        raise RuntimeError("err.acct.profile_push_failed") from ex
    res = res if isinstance(res, dict) else {}
    # Node 以 200 + error=not_connected 报「账号已知但连接不在」→ 同 offline 语义
    if str(res.get("error") or "").strip().lower() in ("not_connected", "offline"):
        raise RuntimeError("err.acct.profile_offline")
    return res


def _unwrap_tg_client(client: Any) -> Any:
    """从「client-ish」对象取出能 ``update_profile`` 的 pyrogram client；取不到 → None。

    兼容裸 pyrogram ``Client`` 与 A 线 ``TelegramClient`` 包装器（其 ``.client`` 才是 pyro）。
    """
    if client is None:
        return None
    if hasattr(client, "update_profile"):
        return client
    inner = getattr(client, "client", None)
    if inner is not None and hasattr(inner, "update_profile"):
        return inner
    return None


async def push_telegram(
    client: Any, *, name: str = "", bio: str = "", avatar_path: str = "",
) -> Dict[str, Any]:
    """pyrogram 协议直改 Telegram 官方资料，逐字段 try/except 不互相拖累。

    返回 ``{"applied": {name/status/avatar: True}, "errors": {字段: 原始原因}}``
    （applied/errors 键统一用 name/status/avatar，与 WhatsApp/Node 口径一致，
    status 即 bio）。client 不可用 → ``RuntimeError("err.acct.profile_offline")``。
    改名显式带 ``last_name=""`` 清掉旧姓，避免「新 first + 旧 last」拼接残留。
    所有 RPC 过 ``run_on_client_loop``（主 client 活在主 Telegram loop）。
    """
    pyro = _unwrap_tg_client(client)
    if pyro is None:
        raise RuntimeError("err.acct.profile_offline")
    applied: Dict[str, bool] = {}
    errors: Dict[str, str] = {}
    if name:
        try:
            await run_on_client_loop(
                pyro, lambda: pyro.update_profile(first_name=name, last_name=""))
            applied["name"] = True
        except Exception as ex:  # noqa: BLE001
            errors["name"] = str(ex)[:200]
            logger.debug("[profile_push] telegram 改昵称失败", exc_info=True)
    if bio:
        try:
            await run_on_client_loop(pyro, lambda: pyro.update_profile(bio=bio))
            applied["status"] = True
        except Exception as ex:  # noqa: BLE001
            errors["status"] = str(ex)[:200]
            logger.debug("[profile_push] telegram 改签名失败", exc_info=True)
    if avatar_path:
        try:
            await run_on_client_loop(
                pyro, lambda: pyro.set_profile_photo(photo=avatar_path))
            applied["avatar"] = True
        except Exception as ex:  # noqa: BLE001
            errors["avatar"] = str(ex)[:200]
            logger.debug("[profile_push] telegram 改头像失败", exc_info=True)
    return {"applied": applied, "errors": errors}


async def execute_profile_push(
    platform: str,
    account_id: str,
    *,
    name: str = "",
    status_text: str = "",
    avatar_bytes: Optional[bytes] = None,
    config: Optional[Dict[str, Any]] = None,
    tg_client: Any = None,
    post_json=None,
) -> Dict[str, Any]:
    """平台官方资料推送共用执行器（单号 POST 与批量对齐共用，零 HTTPException）。

    返回 ``{ok, offline, applied, errors, wa_res, tg_client, error_key}``：
    - ``offline=True`` → 账号未连（路由映射 409 / 批量 skipped_offline）；
    - ``ok=False`` 且非 offline → 全字段失败或服务错误（``error_key`` 为 i18n 键）；
    - 成功时 ``tg_client`` 带回供回读（TG）；``wa_res`` 带回 Node 回传（WA）。

    不写 registry / 不计漏斗——审计与计数留在路由层（单号要 raise、批量要吞错，
    两侧策略不同，强行并进本函数会扭曲语义）。
    """
    plat = str(platform or "").lower()
    aid = str(account_id or "")
    cfg = config or {}
    name = str(name or "").strip()
    status_text = str(status_text or "").strip()
    requested = [f for f, v in (("name", name), ("status", status_text),
                                ("avatar", avatar_bytes)) if v]
    empty = {
        "ok": False, "offline": False, "applied": {}, "errors": {},
        "wa_res": {}, "tg_client": None, "error_key": None,
    }
    if plat == "whatsapp":
        try:
            wa_res = await push_whatsapp(
                cfg, aid, name=name, status_text=status_text,
                avatar_bytes=avatar_bytes, post_json=post_json)
        except RuntimeError as ex:
            key = str(ex.args[0] if ex.args else "err.acct.profile_push_failed")
            out = dict(empty)
            out["offline"] = key == "err.acct.profile_offline"
            out["error_key"] = key
            return out
        except Exception:  # noqa: BLE001
            logger.debug("[profile_push] execute WA 异常 acct=%s", aid,
                         exc_info=True)
            out = dict(empty)
            out["error_key"] = "err.acct.profile_push_failed"
            return out
        wa_res = wa_res if isinstance(wa_res, dict) else {}
        applied = {k: True for k, v in (wa_res.get("applied") or {}).items() if v}
        errors = dict(wa_res.get("errors") or {})
        if not applied and not errors and wa_res.get("ok"):
            applied = {f: True for f in requested}
        ok = bool(applied)
        return {
            "ok": ok, "offline": False, "applied": applied, "errors": errors,
            "wa_res": wa_res, "tg_client": None,
            "error_key": None if ok else "err.acct.profile_push_failed",
        }
    if plat == "telegram":
        if tg_client is None:
            out = dict(empty)
            out["offline"] = True
            out["error_key"] = "err.acct.profile_offline"
            return out
        import tempfile
        import os as _os
        avatar_path = ""
        res: Dict[str, Any] = {}
        if avatar_bytes:
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(avatar_bytes)
            tmp.close()
            avatar_path = tmp.name
        try:
            try:
                res = await push_telegram(
                    tg_client, name=name, bio=status_text,
                    avatar_path=avatar_path)
            except RuntimeError as ex:
                key = str(ex.args[0] if ex.args
                          else "err.acct.profile_push_failed")
                out = dict(empty)
                out["offline"] = key == "err.acct.profile_offline"
                out["error_key"] = key
                out["tg_client"] = tg_client
                return out
            except Exception:  # noqa: BLE001
                logger.debug("[profile_push] execute TG 异常 acct=%s", aid,
                             exc_info=True)
                out = dict(empty)
                out["error_key"] = "err.acct.profile_push_failed"
                out["tg_client"] = tg_client
                return out
        finally:
            if avatar_path:
                try:
                    _os.unlink(avatar_path)
                except Exception:  # noqa: BLE001
                    pass
        res = res if isinstance(res, dict) else {}
        applied = {k: True for k, v in (res.get("applied") or {}).items() if v}
        errors = dict(res.get("errors") or {})
        ok = bool(applied)
        return {
            "ok": ok, "offline": False, "applied": applied, "errors": errors,
            "wa_res": {}, "tg_client": tg_client,
            "error_key": None if ok else "err.acct.profile_push_failed",
        }
    out = dict(empty)
    out["error_key"] = "err.acct.profile_platform_manual"
    return out


async def refresh_self_after_push(
    platform: str,
    account_id: str,
    *,
    config: Optional[Dict[str, Any]] = None,
    wa_res: Optional[Dict[str, Any]] = None,
    tg_client: Any = None,
    fallback_name: str = "",
) -> None:
    """推送成功后回读刷新 ``self_*``（best-effort，任何失败静默）。"""
    plat = str(platform or "").lower()
    cfg = config or {}
    try:
        from src.integrations.account_self_profile import (
            enrich_from_fields, enrich_from_user,
        )
        if plat == "whatsapp":
            wr = wa_res or {}
            await enrich_from_fields(
                "whatsapp", account_id,
                name=str(wr.get("pushname") or fallback_name or ""),
                avatar_url=str(wr.get("avatar_url") or ""),
                config=cfg)
        elif plat == "telegram" and tg_client is not None:
            pyro = _unwrap_tg_client(tg_client)
            if pyro is not None and hasattr(pyro, "get_me"):
                async def _tg_readback():
                    me = await pyro.get_me()
                    return await enrich_from_user(
                        "telegram", account_id, me, config=cfg, client=pyro)
                await run_on_client_loop(pyro, _tg_readback)
    except Exception:  # noqa: BLE001
        logger.debug("[profile_push] 回读刷新失败（忽略）", exc_info=True)
