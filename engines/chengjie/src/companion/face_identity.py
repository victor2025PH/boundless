# -*- coding: utf-8 -*-
"""人脸身份客户端 + 入站接线（#333 EREM2H · 2026-09-17）。

链路（B 线 ``persona_reply`` 的 extra_hint 单一消费口接入）：

    入站图 → 176 边车 ``/v1/face/embed``（CPU ArcFace 512 维；外网走官网网关同路径）
        → 与 **人设原型**（相册 face_ref + 已启用本人照的均值，JSON 缓存）比
        → 与 **客户本人原型**（visual_memory 已确认 / ≥2 张一致推断自拍）比
        → 与 **已确认关系人** 比
        → ``visual_identity.classify_identity`` 四分类 → ``identity_note`` 带外一句进 prompt
        → ``visual_memory.record_observation`` 落库（跨轮 / 跨天 / 跨重启）
    文字入站「是我 / that's me / 这是我妹妹」 → 升最近一张带脸观察为 user_confirmed。

配置 ``vision.face_identity``：``enabled``（未写时：有 LAN ``base_url`` 或托管识图网关即开；显式 false / ``hosted_opt_out`` 关）/ ``base_url``（如 http://192.168.0.176:8767）/
``timeout_sec``（8）/ ``min_det_score``（0.5）/ ``persona_proto_max``（6，人设原型最多取几张图）/
``confirm_window_sec``（86400，客户确认只作用于这么久之内的最近一张带脸图）。

纪律：任何失败（服务不可达 / 超时 / 无脸 / 无原型）→ 返回 ``""``，**拟稿零阻断**；
AI 推断永远是 observation（措辞「推断」），客户亲口确认才升事实；不把身份写进
``[图片内容]`` 落库文本（那是 image_observation / 前端消费的 caption），只走 extra_hint。
门禁 ``tests/test_face_identity.py``（HTTP 全 mock）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.companion import visual_identity as vi
from src.companion.visual_memory import (
    VisualMemoryStore, cosine, detect_relation_statement, detect_self_confirmation,
    get_visual_memory_store, mean_vector,
)

logger = logging.getLogger(__name__)

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_FAIL_COOLDOWN_SEC = 60.0
_last_fail_ts: float = 0.0


# ── 进程级观测（2026-09-18 生产首验沉淀）────────────────────────────────────────
# 首验前 17 小时 `[face_identity]` 日志 0 条、库 0 行——「没流量」和「链在哪一环静默断了」
# 长得一模一样（本函数 7 个提前返回点全部返回 ''）。这里按**退出原因**计数 + 边车耗时分位，
# 由 /api/visual-memory/status 暴露；``no_path`` 首次命中打 WARNING（带 ref 前缀，不带内容），
# 因为那是「平台把图落了库但身份层拿不到文件」的唯一信号（WA RPA 线就是这种盲区）。
_STAT_KEYS = ("calls", "enabled_off", "no_cid", "no_store", "not_image", "no_path", "service_fail",
              "no_face", "face_ok", "persona", "customer_self", "known", "unknown", "repeat",
              "confirm_yes", "confirm_no", "relation", "proto_built", "proto_cached", "proto_none")
_stats: Dict[str, int] = {k: 0 for k in _STAT_KEYS}
_embed_ms: List[float] = []      # 最近 N 次边车往返耗时（滚动）
_EMBED_MS_KEEP = 200
_stats_lock = __import__("threading").Lock()
_last_ok_ts: float = 0.0
_last_error: str = ""
_no_path_warned: bool = False


def _bump(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


def _note_embed_ms(ms: float) -> None:
    global _last_ok_ts
    with _stats_lock:
        _embed_ms.append(float(ms))
        if len(_embed_ms) > _EMBED_MS_KEEP:
            del _embed_ms[: len(_embed_ms) - _EMBED_MS_KEEP]
        _last_ok_ts = time.time()


def _pct(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return float(sorted_vals[i])


def stats() -> Dict[str, Any]:
    """观测快照：各退出原因计数、边车耗时 p50/p95/max、最近成功时刻、最近错误、冷却状态。"""
    with _stats_lock:
        counts = dict(_stats)
        ms = sorted(_embed_ms)
        last_ok = _last_ok_ts
        last_err = _last_error
    now = time.time()
    return {
        "counts": counts,
        "embed_ms": {"n": len(ms), "p50": round(_pct(ms, 0.5), 1), "p95": round(_pct(ms, 0.95), 1),
                     "max": round(ms[-1], 1) if ms else 0.0},
        "last_ok_age_sec": int(now - last_ok) if last_ok else None,
        "last_error": last_err,
        "cooldown_active": bool(_last_fail_ts and now - _last_fail_ts < _FAIL_COOLDOWN_SEC),
    }


def reset_stats() -> None:
    """测试用。"""
    global _last_ok_ts, _last_error, _no_path_warned
    with _stats_lock:
        for k in _STAT_KEYS:
            _stats[k] = 0
        _embed_ms.clear()
        _last_ok_ts = 0.0
        _last_error = ""
    _no_path_warned = False


# ── 配置 ────────────────────────────────────────────────────────────────────

def _tri_bool(val: Any) -> Optional[bool]:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("false", "0", "no", "off"):
        return False
    if s in ("true", "1", "yes", "on"):
        return True
    return None


def face_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``vision.face_identity`` 归一（缺省全给）。"""
    v = ((config or {}).get("vision") or {}) if isinstance(config, dict) else {}
    raw = v.get("face_identity") if isinstance(v.get("face_identity"), dict) else {}
    base = str(raw.get("base_url") or os.environ.get("AITR_FACE_BASE_URL") or "").strip().rstrip("/")
    api_key = str(raw.get("api_key") or "").strip()
    # 托管形态（外网坐席机）：识图已被 hosted_gateway 指向官网网关（vision._hosted_vision，
    # base_url=https://bd2026.cc/api/ai/v1，api_key=cx.* 设备令牌）→ 人脸边车走同一网关
    # 的 /api/ai/v1/face/embed（网关经 117→VPS 隧道回 176:8767）。base_url 去掉尾部 /v1
    # （客户端自己拼 /v1/face/embed），令牌同源。运行时按当前 vision.* 现算 → 热重载 /
    # LAN 回内网还原直连时自动跟随，零 config_manager 改动。显式 base_url 永远优先。
    if not base and v.get("_hosted_vision"):
        gw = str(v.get("base_url") or "").strip().rstrip("/")
        if gw.endswith("/v1"):
            gw = gw[: -len("/v1")]
        if gw.startswith(("http://", "https://")):
            base = gw
            api_key = api_key or str(v.get("api_key") or "").strip()
    try:
        timeout = float(raw.get("timeout_sec", 8) or 8)
    except (TypeError, ValueError):
        timeout = 8.0
    try:
        min_det = float(raw.get("min_det_score", 0.5) or 0.5)
    except (TypeError, ValueError):
        min_det = 0.5
    explicit = _tri_bool(raw.get("enabled")) if "enabled" in raw else None
    opt_out = bool(_tri_bool(raw.get("hosted_opt_out")) is True)
    # 干净包不写 enabled：有 LAN 边车或托管网关即开。显式 false / hosted_opt_out 关。
    if explicit is False or opt_out:
        on = False
    else:
        on = bool(base)
    return {
        "enabled": on,
        "base_url": base,
        "api_key": api_key,
        "hosted": bool(not str(raw.get("base_url") or "").strip() and v.get("_hosted_vision") and base),
        "timeout_sec": max(1.0, min(timeout, 60.0)),
        "min_det_score": max(0.1, min(min_det, 0.95)),
        "persona_proto_max": int(raw.get("persona_proto_max", 6) or 6),
        "confirm_window_sec": float(raw.get("confirm_window_sec", 86400) or 86400),
    }


def face_on(config: Optional[Dict[str, Any]]) -> bool:
    """接线口用：与 ``face_cfg`` 同一套门禁（托管自动开，不读 yaml 裸 ``enabled``）。"""
    return bool(face_cfg(config).get("enabled"))


# ── HTTP 客户端（stdlib，同步；调用侧 to_thread）───────────────────────────────

class FaceEmbedClient:
    def __init__(self, base_url: str, *, timeout_sec: float = 8.0, min_det_score: float = 0.5,
                 api_key: str = "", opener: Optional[Callable[..., Any]] = None) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = float(timeout_sec)
        self.min_det_score = float(min_det_score)
        self.api_key = str(api_key or "").strip()
        self._open = opener or urllib.request.urlopen

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h: Dict[str, str] = dict(extra or {})
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"   # 网关形态：cx.* 设备令牌鉴权
        return h

    def _post(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        global _last_fail_ts, _last_error
        if not self.base_url:
            return None
        if _last_fail_ts and time.time() - _last_fail_ts < _FAIL_COOLDOWN_SEC:
            return None   # 边车刚失败过：60s 内不再打（拟稿别为它排队）
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode("utf-8"),
            headers=self._headers({"Content-Type": "application/json"}), method="POST")
        t0 = time.time()
        try:
            with self._open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _note_embed_ms((time.time() - t0) * 1000.0)
            return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            _last_fail_ts = time.time()
            _last_error = f"{type(e).__name__}: {str(e)[:120]}"
            logger.info("[face_identity] embed call failed (%s) → cooldown %ss", type(e).__name__, int(_FAIL_COOLDOWN_SEC))
            return None

    def embed_bytes(self, data: bytes, *, max_faces: int = 3) -> Optional[List[Dict[str, Any]]]:
        """→ faces（大脸优先，每项含 embedding/bbox/det_score）；服务失败 → None；无脸 → []。"""
        if not data:
            return None
        r = self._post("/v1/face/embed", {
            "image_base64": base64.b64encode(data).decode("ascii"),
            "max_faces": int(max_faces), "min_det_score": self.min_det_score,
        })
        if not r or not r.get("ok"):
            return None
        faces = r.get("faces") or []
        return [f for f in faces if isinstance(f, dict) and f.get("embedding")]

    def embed_path(self, path: Any, *, max_faces: int = 3) -> Optional[List[Dict[str, Any]]]:
        try:
            p = Path(str(path))
            if not p.is_file() or p.stat().st_size > 12 * 1024 * 1024:
                return None
            return self.embed_bytes(p.read_bytes(), max_faces=max_faces)
        except Exception:
            return None

    def health(self) -> bool:
        try:
            with self._open(urllib.request.Request(self.base_url + "/health", headers=self._headers()),
                            timeout=min(self.timeout, 4.0)) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            return bool(d.get("ok"))
        except Exception:
            return False


def reset_fail_cooldown() -> None:
    global _last_fail_ts
    _last_fail_ts = 0.0


# ── 人设原型（face_ref + 相册本人照 → 均值；JSON 缓存按来源 mtime 失效）──────────

def _album_root(config: Optional[Dict[str, Any]], config_path: Any = None) -> Path:
    rel = "assets/persona_media"
    try:
        prov = (((config or {}).get("companion") or {}).get("selfie") or {}).get("provider") or {}
        rel = str(prov.get("album_dir") or rel)
    except Exception:
        pass
    root = Path.cwd()
    try:
        if config_path:
            root = Path(str(config_path)).resolve().parent.parent
    except Exception:
        pass
    base = Path(rel)
    return base if base.is_absolute() else root / base


def persona_source_images(pid: str, config: Optional[Dict[str, Any]], *, config_path: Any = None,
                          max_n: int = 6, store: Any = None) -> List[Path]:
    """人设原型的来源图：face_ref 优先 + 注册相册已启用图（最多 max_n）。"""
    out: List[Path] = []
    safe = "".join(ch for ch in str(pid or "") if ch.isalnum() or ch in "-_.")
    if not safe:
        return out
    try:
        d = _album_root(config, config_path) / safe
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file() and p.stem.lower() == "face_ref" and p.suffix.lower() in _IMAGE_EXT:
                    out.append(p)
                    break
    except Exception:
        pass
    try:
        st = store
        if st is None:
            from src.companion.persona_media_store import get_persona_media_store
            st = get_persona_media_store()
        if st is not None:
            for row in st.list(pid, enabled_only=True):
                mt = str(row.get("media_type") or "").lower()
                if mt not in ("image", "photo", "picture"):
                    continue
                fp = Path(str(row.get("file_path") or ""))
                if fp.is_file() and fp.suffix.lower() in _IMAGE_EXT and fp not in out:
                    out.append(fp)
                if len(out) >= max_n:
                    break
    except Exception:
        logger.debug("[face_identity] persona album listing failed", exc_info=True)
    return out[:max_n]


def _proto_cache_dir() -> Path:
    try:
        from src.licensing.data_paths import config_dir
        return Path(config_dir()) / "face_prototypes"
    except Exception:
        return Path("config") / "face_prototypes"


def persona_prototype(pid: str, config: Optional[Dict[str, Any]], client: FaceEmbedClient, *,
                      config_path: Any = None, max_n: int = 6, store: Any = None,
                      cache_dir: Optional[Path] = None) -> Optional[List[float]]:
    """人设人脸原型（L2 归一均值）。来源图 mtime 集合不变 → 读缓存；变了 → 重算。无脸 → None。"""
    srcs = persona_source_images(pid, config, config_path=config_path, max_n=max_n, store=store)
    if not srcs:
        _bump("proto_none")
        return None
    sig = {str(p): int(p.stat().st_mtime) for p in srcs if p.exists()}
    cdir = Path(cache_dir) if cache_dir else _proto_cache_dir()
    cfile = cdir / f"persona_{''.join(ch for ch in str(pid) if ch.isalnum() or ch in '-_.')}.json"
    try:
        if cfile.is_file():
            cached = json.loads(cfile.read_text(encoding="utf-8"))
            if cached.get("sources") == sig and cached.get("vector"):
                _bump("proto_cached")
                return [float(x) for x in cached["vector"]]
    except Exception:
        pass
    _bump("proto_built")
    vecs: List[List[float]] = []
    used: List[str] = []
    for p in srcs:
        faces = client.embed_path(p, max_faces=1)
        if faces is None:
            return None   # 服务失败：不写缓存，本轮放弃（下次再算）
        if faces:
            vecs.append([float(x) for x in faces[0]["embedding"]])
            used.append(str(p))
    # 相册里混进「换人」图：只保留与 face_ref（首图）一致的
    if len(vecs) >= 2:
        base = vecs[0]
        vecs = [v for v in vecs if cosine(base, v) >= vi.AMBIGUOUS_THRESHOLD]
    vec = mean_vector(vecs)
    if vec is None:
        return None
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        cfile.write_text(json.dumps({"pid": pid, "sources": sig, "used": used, "vector": vec,
                                     "updated": time.time()}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("[face_identity] prototype cache write failed", exc_info=True)
    return vec


# ── 身份判定（纯组合）─────────────────────────────────────────────────────────

def identify_face(face_vec: Sequence[float], *, persona_vec: Optional[Sequence[float]] = None,
                  self_vec: Optional[Sequence[float]] = None,
                  relations: Optional[Sequence[Dict[str, Any]]] = None) -> Tuple[str, str, float, Dict[str, float]]:
    scores: Dict[str, float] = {}
    if persona_vec:
        scores["persona"] = cosine(face_vec, persona_vec)
    if self_vec:
        scores["customer_self"] = cosine(face_vec, self_vec)
    for e in relations or []:
        emb = e.get("embedding")
        rel = str(e.get("relation") or e.get("label") or "")
        if emb and rel:
            scores[rel] = max(scores.get(rel, -1.0), cosine(face_vec, emb))
    label, matched, score = vi.classify_identity(scores, has_face=True)
    return label, matched, score, scores


def _resolve_media_path(media_ref: str) -> str:
    ref = str(media_ref or "").strip()
    if not ref:
        return ""
    try:
        from src.integrations.protocol_bridge import static_media_ref_to_path
        p = static_media_ref_to_path(ref)
        if p and Path(p).is_file():
            return str(p)
    except Exception:
        pass
    try:
        if Path(ref).is_file():
            return ref
    except Exception:
        pass
    return ""


def annotate_inbound_sync(
    *, config: Optional[Dict[str, Any]], conversation_id: str, persona_id: str = "",
    media_type: str = "", media_ref: str = "", message_id: str = "", caption: str = "",
    peer_text: str = "", client: Optional[FaceEmbedClient] = None,
    memory: Optional[VisualMemoryStore] = None, config_path: Any = None,
    persona_store: Any = None, proto_cache_dir: Optional[Path] = None,
) -> str:
    """一轮入站 → 带外身份说明（'' = 无话可说 / 未启用 / 失败）。同步实现；异步侧 to_thread。"""
    global _no_path_warned
    _bump("calls")
    cfg = face_cfg(config)
    if not cfg["enabled"]:
        _bump("enabled_off")
        return ""
    ck = str(conversation_id or "").strip()
    if not ck:
        _bump("no_cid")
        return ""
    mem = memory if memory is not None else get_visual_memory_store()
    if mem is None:
        _bump("no_store")
        return ""
    text = str(peer_text or "")
    is_image = str(media_type or "").lower() in ("image", "photo", "picture") or "[图片内容]" in text

    # ① 文字确认 / 否认 / 关系陈述（不论本轮有没有图）
    try:
        rel = detect_relation_statement(text)
        yn = detect_self_confirmation(text)
        win = float(cfg["confirm_window_sec"])
        if rel and mem.confirm_relation(ck, rel, within_sec=win):
            _bump("relation")
            logger.info("[face_identity] conv=%s relation confirmed=%s", ck, rel)
        elif yn == "yes" and mem.confirm_self(ck, within_sec=win):
            _bump("confirm_yes")
            logger.info("[face_identity] conv=%s self confirmed by customer", ck)
        elif yn == "no" and mem.deny_self(ck, within_sec=win):
            _bump("confirm_no")
            logger.info("[face_identity] conv=%s self denied by customer", ck)
    except Exception:
        logger.debug("[face_identity] confirmation step failed", exc_info=True)

    if not is_image:
        _bump("not_image")
        return ""
    path = _resolve_media_path(media_ref)
    if not path:
        _bump("no_path")
        if not _no_path_warned:
            # 只警一次/进程：平台已判定这是图片却给不出可读文件——身份层在该渠道整体是盲的。
            _no_path_warned = True
            _ref = str(media_ref or "")
            logger.warning("[face_identity] conv=%s 入站图无法解析为本地文件（media_type=%s ref_prefix=%r len=%d）"
                           "——该渠道身份层不工作，后续同类只计数不再告警", ck, media_type, _ref[:40], len(_ref))
        return ""
    cl = client or FaceEmbedClient(cfg["base_url"], timeout_sec=cfg["timeout_sec"],
                                   min_det_score=cfg["min_det_score"], api_key=cfg["api_key"])
    faces = cl.embed_path(path, max_faces=3)
    if faces is None:
        _bump("service_fail")
        return ""   # 服务不可达 / 超时：拟稿照旧，不注入

    fields = vi.parse_caption_fields(caption)
    summary = vi.observation_summary(fields)
    try:
        raw = Path(path).read_bytes()
        sha1 = hashlib.sha1(raw).hexdigest()
    except Exception:
        raw, sha1 = b"", ""
    # #333 P3-4：感知哈希 → 「这张图 TA 之前发过」（sha1 只认字节级重传；转发/截图/重编码靠 pHash）。
    # 复用 image_phash（numpy DCT，无新依赖），任何失败 → ""＝不判复现。
    phash = ""
    try:
        from src.companion.image_phash import phash_bytes
        phash = phash_bytes(raw) if raw else ""
    except Exception:
        phash = ""
    prev = mem.find_repeat(ck, sha1=sha1, phash=phash, exclude_message_id=message_id)
    rep_note = ""
    if prev is not None:
        _bump("repeat")
        from src.companion.visual_memory import _age_label
        rep_note = vi.repeat_note(prev, age_label=_age_label(time.time() - float(prev.get("ts") or 0)),
                                  has_face=bool(faces))
        logger.info("[face_identity] conv=%s repeat by=%s dist=%s prev_ts=%.0f label=%s", ck,
                    prev.get("repeat_by"), prev.get("repeat_dist"), float(prev.get("ts") or 0), prev.get("label"))
    if not faces:
        _bump("no_face")
        mem.record_observation(ck, message_id=message_id, sha1=sha1, subject=fields.get("subject") or "",
                               summary=summary, label="no_face", phash=phash)
        logger.info("[face_identity] conv=%s faces=0 label=no_face subject=%s", ck, fields.get("subject") or "-")
        return rep_note
    _bump("face_ok")

    face_vec = [float(x) for x in faces[0]["embedding"]]
    persona_vec = None
    if persona_id:
        try:
            persona_vec = persona_prototype(persona_id, config, cl, config_path=config_path,
                                            max_n=int(cfg["persona_proto_max"]), store=persona_store,
                                            cache_dir=proto_cache_dir)
        except Exception:
            persona_vec = None
    self_vec, self_src = mem.self_prototype(ck)
    relations = mem.known_relations(ck)
    label, matched, score, scores = identify_face(face_vec, persona_vec=persona_vec, self_vec=self_vec,
                                                  relations=relations)
    confirmed = False
    if label == "customer_self":
        confirmed = (self_src == "user_confirmed")
    elif label == "unknown" and fields.get("is_selfie") and fields.get("person_count", 1) <= 1 \
            and "persona" not in (matched,):
        # 单人自拍 + 不像人设 + 没有本人原型：按「推断是本人」记（不确认；≥2 张一致才成临时原型）
        if self_vec is None or scores.get("customer_self", 0.0) < vi.AMBIGUOUS_THRESHOLD:
            label, matched = "customer_self", "customer_self"
            confirmed = False
    relation = ""
    if label == "known":
        relation = matched
    mem.record_observation(ck, message_id=message_id, sha1=sha1, subject=fields.get("subject") or "",
                           summary=summary, label=label, matched=matched, score=score,
                           embedding=face_vec, source="ai_inferred", confirmed=False, phash=phash)
    if label in _STAT_KEYS:
        _bump(label)
    logger.info("[face_identity] conv=%s faces=%d label=%s matched=%s score=%.3f scores=%s",
                ck, len(faces), label, matched or "-", score,
                {k: round(v, 3) for k, v in scores.items()})
    note = vi.identity_note(label, matched=matched, confirmed=confirmed, relation=relation)
    if len(faces) > 1 and note:
        note += f"（画面里共 {len(faces)} 张脸，以上说的是最大那张）"
    if rep_note and label != "persona":
        # 人设自己的照片被回传：身份说明已经含「多半是我方发过的」，再加「发过」反而混淆归属
        note = f"{note}{rep_note}" if note else rep_note
    return note


# ── 启动预热（#333 P3-3，2026-09-18）─────────────────────────────────────────────

def warmup_persona_ids(config: Optional[Dict[str, Any]], *, config_path: Any = None,
                       store: Any = None, max_n: int = 32) -> List[str]:
    """要预热的人设：相册根下有目录的 + 注册相册里有已启用图的（去重、排序、封顶）。"""
    ids: List[str] = []
    seen: set = set()
    try:
        root = _album_root(config, config_path)
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if d.is_dir() and not d.name.startswith((".", "_")) and d.name not in seen:
                    seen.add(d.name)
                    ids.append(d.name)
    except Exception:
        pass
    try:
        st = store
        if st is None:
            from src.companion.persona_media_store import get_persona_media_store
            st = get_persona_media_store()
        rows = st.list(None, enabled_only=True) if st is not None else []
        for r in rows or []:
            pid = str(r.get("persona_id") or "").strip()
            if pid and pid not in seen and str(r.get("media_type") or "").lower() in ("image", "photo", "picture"):
                seen.add(pid)
                ids.append(pid)
    except Exception:
        logger.debug("[face_identity] warmup persona listing via store failed", exc_info=True)
    return ids[:max_n]


def warmup_persona_prototypes(config: Optional[Dict[str, Any]], *, config_path: Any = None,
                              client: Optional[FaceEmbedClient] = None, store: Any = None,
                              cache_dir: Optional[Path] = None) -> Dict[str, int]:
    """启动后把常驻人设的人脸原型算好落 JSON 缓存——首验实测首图 persona_prototype 冷算
    ~600ms（6 张图各打一次边车），预热后首图只剩本图一次往返（~0.4s → ~0.15s）。

    阻塞式（调用方放后台线程）。未启用 / 边车不健康 → 直接返回，不重试（下一次入站图会
    照旧现算）。任何异常吞掉，只影响首图延迟。返回 {"personas": N, "built": n1, "cached": n2, "none": n3}。"""
    out = {"personas": 0, "built": 0, "cached": 0, "none": 0}
    try:
        cfg = face_cfg(config)
        if not cfg["enabled"]:
            return out
        cl = client or FaceEmbedClient(cfg["base_url"], timeout_sec=cfg["timeout_sec"],
                                       min_det_score=cfg["min_det_score"], api_key=cfg["api_key"])
        if not cl.health():
            logger.info("[face_identity] warmup skipped: sidecar not healthy (%s)", cfg["base_url"])
            return out
        pids = warmup_persona_ids(config, config_path=config_path, store=store)
        out["personas"] = len(pids)
        t0 = time.time()
        for pid in pids:
            before = dict(_stats)
            vec = persona_prototype(pid, config, cl, config_path=config_path,
                                    max_n=int(cfg["persona_proto_max"]), store=store, cache_dir=cache_dir)
            if vec is None:
                out["none"] += 1
            elif _stats.get("proto_cached", 0) > before.get("proto_cached", 0):
                out["cached"] += 1
            else:
                out["built"] += 1
        logger.info("[face_identity] warmup done personas=%d built=%d cached=%d none=%d in %.1fs",
                    out["personas"], out["built"], out["cached"], out["none"], time.time() - t0)
    except Exception:
        logger.debug("[face_identity] warmup failed", exc_info=True)
    return out


def warmup_persona_prototypes_async(config: Optional[Dict[str, Any]], *, config_path: Any = None) -> Any:
    """后台 daemon 线程 fire-and-forget（与 avatar_voice.warmup_personas_async 同模式）。返回线程句柄。"""
    import threading
    t = threading.Thread(target=lambda: warmup_persona_prototypes(config, config_path=config_path),
                         name="face-identity-warmup", daemon=True)
    t.start()
    return t


async def annotate_inbound(**kwargs: Any) -> str:
    """异步包装：同步 HTTP 放线程，任何异常回 ''。"""
    try:
        return await asyncio.to_thread(annotate_inbound_sync, **kwargs)
    except Exception:
        logger.debug("[face_identity] annotate_inbound failed", exc_info=True)
        return ""


__all__ = [
    "face_cfg", "face_on", "FaceEmbedClient", "persona_source_images", "persona_prototype", "identify_face",
    "annotate_inbound_sync", "annotate_inbound", "reset_fail_cooldown", "stats", "reset_stats",
]
