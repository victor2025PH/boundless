"""Persona voice evaluation console routes (promoted from tmp_voice_eval, 2026-08-19).

Endpoints (admin-only):
- GET  /admin/voice-eval                  page (session auth)
- GET  /api/admin/voice-eval/state        manifest + saved ratings
- POST /api/admin/voice-eval/rate         upsert one rating {id, score?, verdict?, note?}
- GET  /api/admin/voice-eval/audio/{name} serve one sample wav (whitelisted basename)

Data lives in the writable instance data area: ``config_dir()/voice_eval/``
(manifest.json + ratings.json + *.wav) so the repo tree stays clean and the
desktop/packaged deployments keep working. Sample generation stays an offline
tooling concern (see tools/ sampling discipline); this console only reads
samples and persists human verdicts.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

_LOCK = threading.Lock()
_SAFE_WAV = re.compile(r"^[\w.\-]+\.wav$", re.UNICODE)


def _data_dir() -> Path:
    from src.licensing.data_paths import config_dir
    return Path(config_dir()) / "voice_eval"


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def register_voice_eval_routes(app, page_auth, api_auth, templates) -> None:
    """Register the voice evaluation console (page + API)."""

    @app.get("/admin/voice-eval")
    async def voice_eval_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "voice_eval.html", {})

    @app.get("/api/admin/voice-eval/state")
    async def voice_eval_state(request: Request, _=Depends(api_auth)):
        d = _data_dir()
        return {
            "manifest": _load_json(d / "manifest.json", {}),
            "ratings": _load_json(d / "ratings.json", {}),
        }

    @app.post("/api/admin/voice-eval/rate")
    async def voice_eval_rate(request: Request, _=Depends(api_auth)):
        from src.web.web_i18n import tr
        try:
            entry = await request.json()
        except Exception:
            entry = {}
        pid = str((entry or {}).get("id") or "").strip()
        if not pid or len(pid) > 120:
            raise HTTPException(
                status_code=400,
                detail=tr(request, "err.ws.field_required", field="id"))
        d = _data_dir()
        d.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            data = _load_json(d / "ratings.json", {})
            row = data.get(pid) or {}
            for k in ("score", "verdict", "note"):
                if k in entry:
                    row[k] = entry[k]
            row["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            data[pid] = row
            tmp = d / "ratings.json.tmp"
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           "utf-8")
            os.replace(tmp, d / "ratings.json")
        return {"ok": True, "row": row}

    @app.get("/api/admin/voice-eval/audio/{name}")
    async def voice_eval_audio(name: str, request: Request, _=Depends(api_auth)):
        base = os.path.basename(str(name or ""))
        f = _data_dir() / base
        if not _SAFE_WAV.match(base) or not f.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return Response(content=f.read_bytes(), media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})
