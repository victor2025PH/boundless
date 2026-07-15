# -*- coding: utf-8 -*-
"""
视频生成队列服务 — LivePortrait 离线队列
===========================================
流程: 文字/音频 + 人脸图 → TTS → RVC → LivePortrait → 输出视频文件
支持: 提交任务/查询进度/取消/重试/完成通知
端口: 通过 avatar_hub.py 的 /api/video/* 代理
"""
import sys, os, json, time, uuid, threading, subprocess, base64, tempfile
from pathlib import Path
from enum import Enum
from typing import Optional

import app_config
BASE_DIR   = app_config.BASE
VIDEO_DIR  = BASE_DIR / "video_output"
VIDEO_DIR.mkdir(exist_ok=True)
QUEUE_FILE = BASE_DIR / "video_queue.json"

class TaskStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    CANCELLED = "cancelled"

# ── 内存任务队列 ─────────────────────────────────────────────────
_tasks: dict = {}   # task_id -> task_dict
_queue: list = []   # 待处理 task_id 列表（FIFO）
_lock  = threading.Lock()
_worker_started = False


def _save_queue():
    try:
        data = {tid: {k: v for k, v in t.items() if k != "cancel_event"}
                for tid, t in _tasks.items()}
        QUEUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding='utf-8')
    except Exception:
        pass


def _load_queue():
    """启动时恢复任务状态。
    - PENDING/RUNNING → 重置为PENDING并重新入队（服务重启前未完成）
    - DONE/FAILED/CANCELLED → 仅恢复到内存字典供查询，不重新入队
    """
    if not QUEUE_FILE.exists():
        return
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding='utf-8'))
        for tid, t in data.items():
            t["cancel_event"] = threading.Event()
            status = t.get("status", "")
            if status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                t["status"] = TaskStatus.PENDING   # 重启后重置为待处理
                _tasks[tid] = t
                _queue.append(tid)          # 只有PENDING才入队
            else:
                _tasks[tid] = t             # DONE/FAILED/CANCELLED仅供查询
    except Exception:
        pass


# ── 任务提交 ─────────────────────────────────────────────────────
def submit_task(text: str, profile_name: str, face_b64: str,
                language: str = "zh-cn", duration_hint: int = 10) -> str:
    """提交视频生成任务，返回 task_id"""
    tid = str(uuid.uuid4())[:8]
    task = {
        "id":           tid,
        "text":         text,
        "profile":      profile_name,
        "face_b64":     face_b64,
        "language":     language,
        "duration_hint": duration_hint,
        "status":       TaskStatus.PENDING,
        "progress":     0,
        "message":      "等待处理",
        "created_at":   time.time(),
        "started_at":   None,
        "finished_at":  None,
        "output_file":  "",
        "error":        "",
        "cancel_event": threading.Event(),
    }
    with _lock:
        _tasks[tid] = task
        _queue.append(tid)
        _save_queue()
    _ensure_worker()
    return tid


def get_task(tid: str) -> Optional[dict]:
    t = _tasks.get(tid)
    if not t:
        return None
    return {k: v for k, v in t.items() if k != "cancel_event"}


def cancel_task(tid: str) -> bool:
    t = _tasks.get(tid)
    if not t:
        return False
    if t["status"] in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return False
    t["cancel_event"].set()
    if t["status"] == TaskStatus.PENDING:
        t["status"] = TaskStatus.CANCELLED
        t["message"] = "已取消"
        with _lock:
            if tid in _queue:
                _queue.remove(tid)
        _save_queue()
    return True


def list_tasks(limit: int = 20) -> list:
    tasks = sorted(_tasks.values(), key=lambda x: x["created_at"], reverse=True)
    return [{k: v for k, v in t.items() if k != "cancel_event"}
            for t in tasks[:limit]]


# ── Worker 线程 ──────────────────────────────────────────────────
def _ensure_worker():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        t = threading.Thread(target=_worker_loop, daemon=True)
        t.start()


def _worker_loop():
    """单线程Worker，按队列顺序处理任务"""
    while True:
        tid = None
        with _lock:
            if _queue:
                tid = _queue.pop(0)
        if tid is None:
            time.sleep(2)
            continue
        task = _tasks.get(tid)
        if not task or task["status"] == TaskStatus.CANCELLED:
            continue
        _process_task(task)


def _update(task, status=None, progress=None, message=None,
            output_file=None, error=None):
    if status:       task["status"]      = status
    if progress is not None: task["progress"] = progress
    if message:      task["message"]     = message
    if output_file:  task["output_file"] = output_file
    if error:        task["error"]       = error
    _save_queue()


def _process_task(task: dict):
    """执行单个任务：TTS → RVC → LivePortrait"""
    import httpx

    tid = task["id"]
    cancel = task["cancel_event"]
    task["status"]     = TaskStatus.RUNNING
    task["started_at"] = time.time()
    _update(task, message="TTS合成中...", progress=10)

    try:
        # ── Step 1: TTS ──────────────────────────────────────────
        if cancel.is_set():
            _update(task, status=TaskStatus.CANCELLED, message="已取消"); return

        import requests as _req
        from pathlib import Path as _P
        profiles_file = BASE_DIR / "avatar_profiles.json"
        profile_data  = {}
        if profiles_file.exists():
            all_profiles = json.loads(profiles_file.read_text(encoding='utf-8'))
            profile_data = all_profiles.get(task["profile"], {})

        voice_b64  = profile_data.get("voice_b64", "")
        voice_name = profile_data.get("voice_name", "") or "female_01.wav"
        lang       = task["language"]
        text       = task["text"]

        tts_url = "http://127.0.0.1:7851"
        if voice_b64:
            r = _req.post(f"{tts_url}/v1/audio/clone",
                json={"text": text, "language": lang,
                      "reference_audio_base64": voice_b64}, timeout=60)
            audio_b64 = r.json().get("audio_base64", "") if r.status_code==200 else ""
            wav_bytes = base64.b64decode(audio_b64) if audio_b64 else b""
        else:
            r = _req.post(f"{tts_url}/v1/audio/speech",
                json={"model": "xtts_v2", "input": text,
                      "voice": voice_name, "language": lang}, timeout=60)
            wav_bytes = r.content if r.status_code==200 else b""

        if not wav_bytes:
            raise ValueError(f"TTS失败: HTTP {r.status_code}")

        _update(task, progress=30, message="RVC音色转换中...")

        # ── Step 2: RVC ──────────────────────────────────────────
        rvc_model    = profile_data.get("rvc_model", "")
        rvc_settings = profile_data.get("rvc_settings", {})
        if rvc_model and not cancel.is_set():
            try:
                rvc_r = _req.post("http://127.0.0.1:6242/convert", json={
                    "audio_base64": base64.b64encode(wav_bytes).decode(),
                    "pth_path":     rvc_model,
                    "index_path":   rvc_settings.get("index_path", ""),
                    "pitch":        rvc_settings.get("pitch", 0),
                    "index_rate":   rvc_settings.get("index_rate", 0.3),
                    "f0method":     rvc_settings.get("f0method", "rmvpe"),
                    "protect":      rvc_settings.get("protect", 0.33),
                }, timeout=60)
                if rvc_r.status_code == 200:
                    ab = rvc_r.json().get("audio_base64", "")
                    if ab: wav_bytes = base64.b64decode(ab)
            except Exception as e:
                pass   # RVC失败用原音继续

        if cancel.is_set():
            _update(task, status=TaskStatus.CANCELLED, message="已取消"); return

        _update(task, progress=50, message="生成视频中(LivePortrait)...")

        # ── Step 3: LivePortrait ─────────────────────────────────
        face_b64 = task.get("face_b64", "")
        out_path = VIDEO_DIR / f"{tid}.mp4"

        lp_ok = _run_live_portrait(wav_bytes, face_b64, out_path, cancel)

        if cancel.is_set():
            _update(task, status=TaskStatus.CANCELLED, message="已取消"); return

        if lp_ok and out_path.exists():
            # 授权可见水印：补齐这条不经 vcam 的离线导出管线（与直播/录制同一策略，音轨 -c:a copy 无损）
            try:
                import watermark as _wm
                _on, _txt = _wm.resolve(force_reload=True)
                if _on:
                    _update(task, progress=95, message="添加授权水印…")
                    _wm.apply_to_mp4(out_path, on=_on, text=_txt)
            except Exception:
                pass
            task["finished_at"] = time.time()
            elapsed = int(task["finished_at"] - task["started_at"])
            _update(task, status=TaskStatus.DONE, progress=100,
                    message=f"完成！耗时{elapsed}秒",
                    output_file=str(out_path))
        else:
            raise ValueError("LivePortrait 生成失败，请检查服务是否安装")

    except Exception as e:
        task["finished_at"] = time.time()
        _update(task, status=TaskStatus.FAILED, progress=0,
                message="失败", error=str(e))


def _run_live_portrait(wav_bytes: bytes, face_b64: str,
                       out_path: Path, cancel: threading.Event) -> bool:
    """
    调用 LivePortrait 生成说话视频
    优先尝试 HTTP API（如果部署了的话），否则命令行
    """
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        # 写临时音频
        audio_path = tmp_dir / "input.wav"
        audio_path.write_bytes(wav_bytes)

        # 写临时人脸图
        face_path = tmp_dir / "face.jpg"
        if face_b64:
            raw = base64.b64decode(face_b64.split(",")[-1] if "," in face_b64 else face_b64)
            face_path.write_bytes(raw)
        else:
            return False

        # 尝试 HTTP API 方式（如果有 LivePortrait 服务）
        try:
            import requests as _req
            r = _req.post("http://127.0.0.1:8010/generate_video", json={
                "audio_base64": base64.b64encode(wav_bytes).decode(),
                "face_base64":  face_b64,
            }, timeout=300)
            if r.status_code == 200:
                out_path.write_bytes(r.content)
                return True
        except Exception:
            pass

        # 命令行方式（fallback，需要 LivePortrait 安装在指定路径）
        lp_script = BASE_DIR / "LivePortrait" / "inference.py"
        if lp_script.exists():
            cmd = [
                sys.executable, str(lp_script),
                "--driving_audio", str(audio_path),
                "--source_image",  str(face_path),
                "--output",        str(out_path),
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            while proc.poll() is None:
                if cancel.is_set():
                    proc.kill(); return False
                time.sleep(1)
            return proc.returncode == 0 and out_path.exists()

        # 两种方式都不可用 → 生成占位说明文件
        out_path.with_suffix(".txt").write_text(
            "LivePortrait 未安装，请参考文档安装后重试", encoding='utf-8')
        return False

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── 初始化 ──────────────────────────────────────────────────────
_load_queue()
_ensure_worker()
