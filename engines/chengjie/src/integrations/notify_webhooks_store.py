"""告警 Webhook 列表存储（Phase 11）。

让运营**不碰 YAML** 也能增删自动回复告警渠道（Telegram / WhatsApp / Messenger /
通用 JSON）：列表写入独立 JSON（``config/notify_webhooks.json``），运行时覆盖
``config.yaml::notify.webhooks``（覆盖层存在则整段取代），并热更
``app.state.webhook_notifier``，无需改 YAML、无需重启。

设计：纯文件 + 内存缓存 + 线程锁；白名单校验（只接受已知字段、合法 format/events）。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: Optional[List[Dict[str, Any]]] = None
# 缓存有效性锚 = 文件 mtime_ns（-1 表示「缓存对应文件不存在」状态不适用/未缓存）。
# 2026-08-02 前 _cache 一经装载永不失效：运营手改 JSON（绕过面板）时 store 与 notifier
# 双双陈旧，且文件被删后仍返回旧列表。现按 mtime 失效（与 config.yaml 热更同哲学）：
# 文件变了重读、文件没了返 None；面板保存路径 save_list 仍即时刷新缓存。
_cache_mtime_ns: int = -1
# 落点单一事实源：与 config.local.yaml / global_rules / personas 同锚 config_dir()
# （AITR_CONFIG_PATH 父 → AITR_DATA_DIR/config → 仓内），不再靠「CWD 恰好是数据根」。
# None = 未显式覆盖 → 走 config_dir()；set_store_path 显式覆盖优先（测试用）。
_path: Optional[Path] = None

_ALLOWED_FORMATS = {
    "telegram", "whatsapp", "messenger", "json",
    "dingtalk", "feishu", "wecom",
}


def _store_path() -> Path:
    """有效落点：显式覆盖优先，否则锚定 ``config_dir()``（与配置家族同目录）。

    历史坑（2026-08-01）：本模块曾是配置家族里唯一用裸相对路径 ``config/…`` 的叛徒，
    靠「服务进程 CWD 恰好是数据根」这个隐式契约。服务实测从引擎根起
    （``python D:\\...\\main.py``）→ overlay 落**引擎根**，而 config.local.yaml 经 env
    落**数据根** ＝ 分裂：面板存了、服务读的却是另一份，引擎根那份还成了诱饵文件。
    改锚 ``config_dir()`` 后与整个配置家族同目录，分裂与诱饵一并消除。
    """
    if _path is not None:
        return _path
    try:
        from src.licensing.data_paths import config_dir
        return config_dir() / "notify_webhooks.json"
    except Exception:
        # licensing 不可用（极端裸环境）→ 回旧相对行为，绝不劣于从前
        return Path("config/notify_webhooks.json")


def set_store_path(p: Optional[Path]) -> None:
    """测试辅助：显式切换存储路径并清缓存；``None`` 复位为默认（config_dir 锚定）。"""
    global _path, _cache, _cache_mtime_ns
    with _lock:
        _path = Path(p) if p is not None else None
        _cache = None
        _cache_mtime_ns = -1


def store_path() -> Path:
    """只读暴露有效落点（自检 API / CLI / 看板用）；不建目录、不触碰文件。"""
    return _store_path()


def _known_event_aliases() -> set:
    try:
        from src.inbox.webhook_notifier import _EVENT_ALIASES
        return set(_EVENT_ALIASES.keys())
    except Exception:
        return {"all"}


def sanitize_webhook(item: Dict[str, Any]) -> Dict[str, Any]:
    """单条 webhook 白名单校验 + 类型规整。非法 format → json；非法事件别名剔除。"""
    if not isinstance(item, dict):
        return {}
    fmt = str(item.get("format") or "json").strip().lower()
    if fmt not in _ALLOWED_FORMATS:
        fmt = "json"
    aliases = _known_event_aliases()
    events = [str(e).strip() for e in (item.get("events") or []) if str(e).strip()]
    events = [e for e in events if e in aliases] or ["autoreply_alert"]
    out: Dict[str, Any] = {
        "name": str(item.get("name") or "webhook").strip()[:64],
        "format": fmt,
        "url": str(item.get("url") or "").strip()[:1000],
        "token": str(item.get("token") or "").strip()[:2000],
        "target": str(item.get("target") or item.get("chat_id") or "").strip()[:128],
        "secret": str(item.get("secret") or "").strip()[:512],
        "events": events,
        "enabled": item.get("enabled", True) is not False,
    }
    # 2026-09-09：链接根地址（把卡片里的 /workspace/... 拼成公网可点的绝对地址）。
    # 此前这里把它丢了 → 面板保存过的渠道永远只剩「查看 AI 花费」几个字没有链接。
    base = str(item.get("base_url") or "").strip()[:300]
    if base.startswith(("http://", "https://")):
        out["base_url"] = base.rstrip("/")
    # Q-14 #262 E（2026-09-10）：链接形态 login（默认）/ magic / off。键缺席 → 不落键（＝login，
    # 且让 merge_preserve_secrets 能区分「面板没这个字段」与「显式选了 login」）；非法值 → 不落键。
    links = str(item.get("links") or "").strip().lower()
    if links in ("login", "magic", "off"):
        out["links"] = links
    # 2026-09-10 运维群降噪 P1.1：渠道最低严重度 info（默认＝全收）/ warning / critical。
    # 老板私聊配 critical → 只收 🔴 与日报/业务类，慢性积压与算力黄灯不再打扰。
    sev = str(item.get("min_severity") or "").strip().lower()
    if sev in ("warning", "critical"):
        out["min_severity"] = sev
    return out


def sanitize_list(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in items[:50]:  # 上限保护
        clean = sanitize_webhook(it)
        if clean:
            out.append(clean)
    return out


def load() -> Optional[List[Dict[str, Any]]]:
    """读覆盖层；文件不存在 → None（表示"未覆盖"，沿用 config.yaml）。

    缓存按文件 mtime_ns 失效：手改文件（绕过面板）下一次 load 即读到新内容、
    文件被删即回 None——store 的「文件真相」口径与磁盘一致，告警自检的
    分歧检测（文件 vs 运行中 notifier）才有意义。坏 JSON 保守忽略覆盖（沿用
    config.yaml），不缓存坏状态（修好文件即恢复）。"""
    global _cache, _cache_mtime_ns
    with _lock:
        p = _store_path()
        try:
            mtime_ns = p.stat().st_mtime_ns
        except OSError:
            _cache = None
            _cache_mtime_ns = -1
            return None
        if _cache is not None and mtime_ns == _cache_mtime_ns:
            return list(_cache)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            _cache = sanitize_list(raw if isinstance(raw, list)
                                   else raw.get("webhooks"))
            _cache_mtime_ns = mtime_ns
            return list(_cache)
        except Exception:
            logger.warning("notify_webhooks.json 解析失败，忽略覆盖", exc_info=True)
            _cache = None
            _cache_mtime_ns = -1
            return None


def save_list(items: Any) -> List[Dict[str, Any]]:
    """整段覆盖写盘（运营面板的权威来源）。返回规整后的列表。"""
    global _cache, _cache_mtime_ns
    clean = sanitize_list(items)
    with _lock:
        p = _store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(clean, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        _cache = clean
        try:
            _cache_mtime_ns = p.stat().st_mtime_ns
        except OSError:
            _cache_mtime_ns = -1
    return list(clean)


def effective_webhooks(base_cfg: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """有效 webhook 列表：覆盖层存在则用覆盖层，否则用 config.yaml::notify.webhooks。"""
    ov = load()
    if ov is not None:
        return ov
    base = ((base_cfg or {}).get("notify") or {}).get("webhooks") or []
    return list(base)


def merge_preserve_secrets(
    incoming: List[Dict[str, Any]],
    old_by_name: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """按 name 沿用旧真实密钥：前端回传脱敏(``abc***``)或空 token/secret 时不覆盖。

    **为什么是安全关键**：运营面板展示的 token/secret 是脱敏的（``mask``），保存时
    前端把脱敏值原样回传。若不还原成旧真值,一次「改个名字再保存」就会把真 token
    覆盖成 ``abc***`` 字面量 → 之后所有告警投递都用错密钥静默失败（保存成功、告警发
    不出,正是最难查的形态）。无同名旧条目时脱敏值清空(绝不把 ``***`` 当密钥存)。

    就地改 incoming 并返回（调用方已 sanitize_list 过,每条含 token/secret 键）。
    """
    for w in incoming:
        old = old_by_name.get(w.get("name")) or {}
        for k in ("token", "secret"):
            nv = str(w.get(k) or "")
            if (not nv) or nv.endswith("***"):
                w[k] = str(old.get(k) or "")
        # Q-14 #262 E：面板表单没有 links 字段——回传缺键时沿用旧值，免得一次「改个名字再保存」
        # 把手工配好的 magic 抹回 login。显式带 links（含 login）则以回传为准。
        if "links" not in w and old.get("links") in ("magic", "off"):
            w["links"] = old["links"]
        if "min_severity" not in w and old.get("min_severity") in ("warning", "critical"):
            w["min_severity"] = old["min_severity"]
    return incoming


def mask(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给前端展示用：脱敏 token/secret，只暴露是否已设置。"""
    out: List[Dict[str, Any]] = []
    for w in items:
        m = dict(w)
        for k in ("token", "secret"):
            v = str(m.get(k) or "")
            m[k] = (v[:3] + "***") if v else ""
            m[f"{k}_set"] = bool(v)
        out.append(m)
    return out
