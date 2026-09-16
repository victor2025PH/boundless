# -*- coding: utf-8 -*-
"""人脸身份客户端 + 入站接线（#333 EREM2H · 2026-09-17）。

链路（B 线 ``persona_reply`` 的 extra_hint 单一消费口接入，默认关）：

    入站图 → 176 边车 ``/v1/face/embed``（CPU ArcFace 512 维）
        → 与 **人设原型**（相册 face_ref + 已启用本人照的均值，JSON 缓存）比
        → 与 **客户本人原型**（visual_memory 已确认 / ≥2 张一致推断自拍）比
        → 与 **已确认关系人** 比
        → ``visual_identity.classify_identity`` 四分类 → ``identity_note`` 带外一句进 prompt
        → ``visual_memory.record_observation`` 落库（跨轮 / 跨天 / 跨重启）
    文字入站「是我 / that's me / 这是我妹妹」 → 升最近一张带脸观察为 user_confirmed。

配置 ``vision.face_identity``：``enabled``（默认 false）/ ``base_url``（如 http://192.168.0.176:8767）/
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


# ── 配置 ────────────────────────────────────────────────────────────────────

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
    return {
        "enabled": bool(raw.get("enabled", False)) and bool(base),
        "base_url": base,
        "api_key": api_key,
        "hosted": bool(not str(raw.get("base_url") or "").strip() and v.get("_hosted_vision") and base),
        "timeout_sec": max(1.0, min(timeout, 60.0)),
        "min_det_score": max(0.1, min(min_det, 0.95)),
        "persona_proto_max": int(raw.get("persona_proto_max", 6) or 6),
        "confirm_window_sec": float(raw.get("confirm_window_sec", 86400) or 86400),
    }


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
        global _last_fail_ts
        if not self.base_url:
            return None
        if _last_fail_ts and time.time() - _last_fail_ts < _FAIL_COOLDOWN_SEC:
            return None   # 边车刚失败过：60s 内不再打（拟稿别为它排队）
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode("utf-8"),
            headers=self._headers({"Content-Type": "application/json"}), method="POST")
        try:
            with self._open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            _last_fail_ts = time.time()
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
        return None
    sig = {str(p): int(p.stat().st_mtime) for p in srcs if p.exists()}
    cdir = Path(cache_dir) if cache_dir else _proto_cache_dir()
    cfile = cdir / f"persona_{''.join(ch for ch in str(pid) if ch.isalnum() or ch in '-_.')}.json"
    try:
        if cfile.is_file():
            cached = json.loads(cfile.read_text(encoding="utf-8"))
            if cached.get("sources") == sig and cached.get("vector"):
                return [float(x) for x in cached["vector"]]
    except Exception:
        pass
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
    cfg = face_cfg(config)
    if not cfg["enabled"]:
        return ""
    ck = str(conversation_id or "").strip()
    if not ck:
        return ""
    mem = memory if memory is not None else get_visual_memory_store()
    if mem is None:
        return ""
    text = str(peer_text or "")
    is_image = str(media_type or "").lower() in ("image", "photo", "picture") or "[图片内容]" in text

    # ① 文字确认 / 否认 / 关系陈述（不论本轮有没有图）
    try:
        rel = detect_relation_statement(text)
        yn = detect_self_confirmation(text)
        win = float(cfg["confirm_window_sec"])
        if rel and mem.confirm_relation(ck, rel, within_sec=win):
            logger.info("[face_identity] conv=%s relation confirmed=%s", ck, rel)
        elif yn == "yes" and mem.confirm_self(ck, within_sec=win):
            logger.info("[face_identity] conv=%s self confirmed by customer", ck)
        elif yn == "no" and mem.deny_self(ck, within_sec=win):
            logger.info("[face_identity] conv=%s self denied by customer", ck)
    except Exception:
        logger.debug("[face_identity] confirmation step failed", exc_info=True)

    if not is_image:
        return ""
    path = _resolve_media_path(media_ref)
    if not path:
        return ""
    cl = client or FaceEmbedClient(cfg["base_url"], timeout_sec=cfg["timeout_sec"],
                                   min_det_score=cfg["min_det_score"], api_key=cfg["api_key"])
    faces = cl.embed_path(path, max_faces=3)
    if faces is None:
        return ""   # 服务不可达 / 超时：拟稿照旧，不注入

    fields = vi.parse_caption_fields(caption)
    summary = vi.observation_summary(fields)
    try:
        sha1 = hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except Exception:
        sha1 = ""
    if not faces:
        mem.record_observation(ck, message_id=message_id, sha1=sha1, subject=fields.get("subject") or "",
                               summary=summary, label="no_face")
        return ""

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
                           embedding=face_vec, source="ai_inferred", confirmed=False)
    logger.info("[face_identity] conv=%s faces=%d label=%s matched=%s score=%.3f scores=%s",
                ck, len(faces), label, matched or "-", score,
                {k: round(v, 3) for k, v in scores.items()})
    note = vi.identity_note(label, matched=matched, confirmed=confirmed, relation=relation)
    if len(faces) > 1 and note:
        note += f"（画面里共 {len(faces)} 张脸，以上说的是最大那张）"
    return note


async def annotate_inbound(**kwargs: Any) -> str:
    """异步包装：同步 HTTP 放线程，任何异常回 ''。"""
    try:
        return await asyncio.to_thread(annotate_inbound_sync, **kwargs)
    except Exception:
        logger.debug("[face_identity] annotate_inbound failed", exc_info=True)
        return ""


__all__ = [
    "face_cfg", "FaceEmbedClient", "persona_source_images", "persona_prototype", "identify_face",
    "annotate_inbound_sync", "annotate_inbound", "reset_fail_cooldown",
]
