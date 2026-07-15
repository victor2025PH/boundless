# -*- coding: utf-8 -*-
"""platform/compliance/client.py — 合规验真『瘦客户端』(纯 stdlib，可降级)。

见 CONTRACT.md：签名/水印在 avatarhub 就地完成(私钥不出机)；本客户端只消费其 HTTP 验真面
(/api/provenance/*)。avatarhub 不在线时**不抛异常**，返回 {"available": False,...}，
让官网/下单/其它引擎能安全地『弱依赖』合规验真。

依赖铁律：只用 stdlib(urllib/json)，不 import engines/products/website，也不 import 第三方包。

用法：
    from client import ComplianceClient
    cc = ComplianceClient()                 # base_url 读环境变量 AVATARHUB_BASE_URL，缺省 127.0.0.1:9000
    if cc.available():
        r = cc.verify_audio(b64_wav)        # {"signature_valid":..., "detection":..., ...}
    st = cc.status()                        # 始终安全：不可达时 {"available": False, ...}
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

_DEFAULT_BASE = os.environ.get("AVATARHUB_BASE_URL", "http://127.0.0.1:9000")


class ComplianceClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 5.0):
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    # ---- 内部 HTTP（可降级：任何失败都收敛为 dict，不抛给调用方）----
    def _get(self, path: str) -> Dict[str, Any]:
        return self._request("GET", path, None)

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", path, payload)

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                out = json.loads(body) if body else {}
                if isinstance(out, dict):
                    out.setdefault("available", True)
                    return out
                return {"available": True, "data": out}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return {"available": False, "error": f"HTTP {e.code}", "detail": detail}
        except Exception as e:  # 连接失败/超时/JSON 解析失败 —— 一律降级
            return {"available": False, "error": str(e)[:200]}

    # ---- 契约方法（见 CONTRACT.md §2）----
    def status(self) -> Dict[str, Any]:
        """/api/provenance/status —— 合规开关/算法/是否可离线验真。"""
        return self._get("/api/provenance/status")

    def pubkey(self) -> Optional[Dict[str, Any]]:
        """/api/provenance/pubkey —— Ed25519 公钥(PEM)，第三方可离线验签。不可用返回 None。"""
        r = self._get("/api/provenance/pubkey")
        return r if r.get("available") else None

    def verify_audio(self, audio_base64: str) -> Dict[str, Any]:
        """/api/provenance/verify —— 验证音频内容凭证(水印+验签+AI生成判定)。"""
        return self._post("/api/provenance/verify", {"audio_base64": audio_base64})

    def verify_media(self, media_base64: str, mime_type: str = "video/mp4") -> Dict[str, Any]:
        """/api/provenance/verify_media —— 验证视频/图片内嵌标准 C2PA。"""
        return self._post("/api/provenance/verify_media",
                          {"media_base64": media_base64, "mime_type": mime_type})

    def manifest(self, payload_id: str) -> Optional[Dict[str, Any]]:
        """/api/provenance/manifest/{id} —— 软绑定解析回溯完整 manifest。找不到/不可达返回 None。"""
        r = self._get("/api/provenance/manifest/" + str(payload_id))
        return r if r.get("available") else None

    def available(self) -> bool:
        """avatarhub 合规面是否就绪(HTTP 可达且 provenance 已加载)。"""
        st = self.status()
        return bool(st.get("available")) and bool(st.get("loaded", False))


def _selftest() -> int:
    cc = ComplianceClient()
    print(f"[compliance.client] base_url={cc.base_url}")
    st = cc.status()
    print(f"  status(): available={st.get('available')} loaded={st.get('loaded')} "
          f"alg={st.get('signature_alg')} note={str(st.get('error',''))[:60]}")
    print(f"  available()={cc.available()}  (avatarhub 未在线属正常，客户端已降级不抛错)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
