# -*- coding: utf-8 -*-
"""真人视频数字人素材构建器(路线A)。

从任意一段半身视频自动构建:
  - <name>_idle_loop.mp4 : 待机循环(最平稳窗口, 18fps, boomerang 无缝)
  - <name>_body.mp4      : 口型底视频(同一窗口, 25fps, 供 MuseTalk 逐帧贴口型)
  - <name>_face.jpg      : 头像(中性锚定帧)

两条核心设计:
  ① 人脸检测 → 计算稳定的 9:16 头肩裁切窗口(人脸落在上中部, 含头顶余量+肩胸)。
  ② 待机与口型底取「同一段最平稳窗口」且都从同一首帧(中性锚定帧)起播 →
     待机↔说话切换处姿态一致, 接缝近乎为零(再叠 vcam 交叉淡入即无缝)。

无人脸时安全回退为居中裁切。纯 CPU(mediapipe + cv2), 几秒内完成。
"""
import os
import shutil
import tempfile
import cv2
import numpy as np

try:
    import mediapipe as _mp
    _MP_FD = _mp.solutions.face_detection
except Exception:
    _MP_FD = None


def _detect_face(fd, frame_bgr):
    """返回 (cx, cy, w, h) 像素坐标的人脸框中心+尺寸; 无脸返回 None。"""
    H, W = frame_bgr.shape[:2]
    if fd is not None:
        try:
            res = fd.process(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
            if res.detections:
                # 取最大(最近)的人脸
                best, area = None, -1.0
                for d in res.detections:
                    bb = d.location_data.relative_bounding_box
                    a = max(0.0, bb.width) * max(0.0, bb.height)
                    if a > area:
                        area, best = a, bb
                x = best.xmin * W
                y = best.ymin * H
                w = best.width * W
                h = best.height * H
                return (x + w / 2, y + h / 2, w, h)
        except Exception:
            pass
    # 回退: haar
    try:
        cas = cv2.CascadeClassifier(
            os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        g = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        faces = cas.detectMultiScale(g, 1.2, 5, minSize=(80, 80))
        if len(faces):
            x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
            return (x + w / 2, y + h / 2, w, h)
    except Exception:
        pass
    return None


def _analyze(cap, src_fps, n_total, analyze_fps=5.0):
    """粗采样: 逐采样帧做人脸检测 + 运动量(相邻采样灰度差)。
    返回 samples=[{t, box|None, motion}], 以及人脸框中位数统计。"""
    # model_selection=0 短距模型: 半身近景大脸检出率显著更高(本场景脸通常 <1m)
    fd = _MP_FD.FaceDetection(model_selection=0, min_detection_confidence=0.4) if _MP_FD else None
    samples = []
    step_t = 1.0 / analyze_fps
    dur = n_total / src_fps if src_fps else 0
    prev_small = None
    t = 0.0
    try:
        while t < dur:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * src_fps))
            ok, fr = cap.read()
            if not ok:
                break
            box = _detect_face(fd, fr)
            small = cv2.resize(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), (48, 48))
            motion = 0.0 if prev_small is None else float(
                np.mean(np.abs(small.astype(np.int16) - prev_small.astype(np.int16))))
            prev_small = small
            samples.append({"t": t, "box": box, "motion": motion})
            t += step_t
    finally:
        if fd is not None:
            fd.close()
    boxes = [s["box"] for s in samples if s["box"] is not None]
    stat = None
    if boxes:
        arr = np.array(boxes)  # cols: cx,cy,w,h
        stat = {
            "cx": float(np.median(arr[:, 0])),
            "cy": float(np.median(arr[:, 1])),
            "w":  float(np.median(arr[:, 2])),
            "h":  float(np.median(arr[:, 3])),
            "face_ratio": len(boxes) / max(1, len(samples)),
        }
    return samples, stat


def _crop_window(stat, FW, FH, aspect):
    """据人脸中位框算 9:16(aspect=W/H) 头肩裁切窗口 (x0,y0,cw,ch)。无脸→居中。"""
    if stat is None:
        ch = FH
        cw = int(round(ch * aspect))
        if cw > FW:
            cw = FW
            ch = int(round(cw / aspect))
        return ((FW - cw) // 2, (FH - ch) // 2, cw, ch)
    fh = stat["h"]
    # 收紧头肩构图: 头顶(≈cy-1.0fh) 到 上胸(≈cy+2.3fh) → 竖向覆盖 ~3.3*fh,
    # 人脸约占画面高 30%(直播级头肩景别, 去掉多余天花板/留头顶适度余量)。
    ch = int(round(fh * 3.3))
    cw = int(round(ch * aspect))
    if cw > FW:                          # 横向超宽 → 以宽为准
        cw = FW
        ch = int(round(cw / aspect))
    if ch > FH:                          # 竖向超高 → 以高为准
        ch = FH
        cw = min(FW, int(round(ch * aspect)))
    # 人脸落在裁切框约 33% 高度处(上中部, 留头顶余量)
    x0 = int(round(stat["cx"] - cw / 2))
    y0 = int(round(stat["cy"] - 0.33 * ch))
    x0 = max(0, min(x0, FW - cw))
    y0 = max(0, min(y0, FH - ch))
    return (x0, y0, cw, ch)


def _calm_window(samples, base_seconds, analyze_fps=5.0):
    """滑窗找平均运动最小、且人脸基本在场的连续窗口, 返回起始秒。"""
    win = max(1, int(base_seconds * analyze_fps))
    if len(samples) <= win:
        return samples[0]["t"] if samples else 0.0
    best_t, best_score = samples[0]["t"], 1e18
    for i in range(0, len(samples) - win):
        seg = samples[i:i + win]
        mot = np.mean([s["motion"] for s in seg])
        face_miss = sum(1 for s in seg if s["box"] is None) / win
        score = mot + face_miss * 8.0   # 缺脸重罚, 避免选到转头/出框段
        if score < best_score:
            best_score, best_t = score, seg[0]["t"]
    return best_t


def _read_window(cap, src_fps, start_t, seconds, out_fps, crop, canvas):
    x0, y0, cw, ch = crop
    OW, OH = canvas
    frames = []
    n = int(seconds * out_fps)
    for i in range(n):
        t = start_t + i / out_fps
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * src_fps))
        ok, fr = cap.read()
        if not ok:
            break
        c = fr[y0:y0 + ch, x0:x0 + cw]
        frames.append(cv2.resize(c, (OW, OH), interpolation=cv2.INTER_AREA))
    return frames


def _write_mp4(path, frames, fps, size):
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for f in frames:
        vw.write(f)
    vw.release()
    return len(frames)


def build_avatar_assets(src_path, out_dir, name, *,
                        canvas=(720, 1280), idle_fps=18, body_fps=25,
                        base_seconds=7.0, analyze_fps=5.0):
    """主入口: 见模块 docstring。返回 dict(idle_video, body_video, face_path, meta)。
    Windows 中文路径安全: 全程在 ASCII 临时目录读写(cv2 的 VideoCapture/VideoWriter/imwrite
    不支持非 ASCII 路径), 完成后用 Python(shutil) 移动到目标目录。"""
    os.makedirs(out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="va_")     # ASCII 临时工作目录
    try:
        return _build_in_workdir(src_path, out_dir, name, work, canvas,
                                 idle_fps, body_fps, base_seconds, analyze_fps)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _build_in_workdir(src_path, out_dir, name, work, canvas,
                      idle_fps, body_fps, base_seconds, analyze_fps):
    src_ext = os.path.splitext(src_path)[1].lower() or ".mp4"
    tmp_src = os.path.join(work, "src" + src_ext)
    shutil.copyfile(src_path, tmp_src)        # 复制到 ASCII 路径再喂 cv2
    cap = cv2.VideoCapture(tmp_src)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {src_path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    FW = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    FH = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    dur = n_total / src_fps if src_fps else 0
    OW, OH = canvas
    aspect = OW / float(OH)

    samples, stat = _analyze(cap, src_fps, n_total, analyze_fps)
    crop = _crop_window(stat, FW, FH, aspect)
    # 平稳窗口不超过素材时长
    seg_sec = min(base_seconds, max(1.0, dur - 0.5))
    anchor_t = _calm_window(samples, seg_sec, analyze_fps)
    # 防越界: 锚点 + 窗口不超尾部
    if anchor_t + seg_sec > dur:
        anchor_t = max(0.0, dur - seg_sec)

    # 待机(boomerang) + 口型底, 都从 anchor_t 起 → 共享中性锚定帧(frame0)
    idle_fwd = _read_window(cap, src_fps, anchor_t, seg_sec, idle_fps, crop, canvas)
    loop = idle_fwd + idle_fwd[-2:0:-1] if len(idle_fwd) >= 3 else idle_fwd
    if len(loop) > 238:                       # vcam 待机帧上限
        loop = loop[:238]
    body = _read_window(cap, src_fps, anchor_t, seg_sec, body_fps, crop, canvas)
    cap.release()

    if len(loop) < 2 or len(body) < 2:
        raise RuntimeError("有效帧不足, 请检查视频(人脸/时长)")

    # 先写 ASCII 临时文件(cv2 不支持非 ASCII 路径), 再移动到目标目录
    idle_tmp = os.path.join(work, "idle_loop.mp4")
    body_tmp = os.path.join(work, "body.mp4")
    face_tmp = os.path.join(work, "face.jpg")
    _write_mp4(idle_tmp, loop, idle_fps, (OW, OH))
    _write_mp4(body_tmp, body, body_fps, (OW, OH))
    cv2.imwrite(face_tmp, body[0])            # 锚定帧 = 头像 = 两序列 frame0

    idle_path = os.path.join(out_dir, f"{name}_idle_loop.mp4")
    body_path = os.path.join(out_dir, f"{name}_body.mp4")
    face_path = os.path.join(out_dir, f"{name}_face.jpg")
    for _s, _d in ((idle_tmp, idle_path), (body_tmp, body_path), (face_tmp, face_path)):
        if not os.path.exists(_s):
            raise RuntimeError("素材写入失败(cv2 编码器不可用?)")
        shutil.move(_s, _d)

    meta = {
        "src_fps": round(src_fps, 2), "duration": round(dur, 1),
        "src_size": [FW, FH], "canvas": [OW, OH],
        "crop": list(crop), "anchor_t": round(anchor_t, 2),
        "face_detected": stat is not None,
        "face_ratio": round(stat["face_ratio"], 2) if stat else 0.0,
        "idle_frames": len(loop), "body_frames": len(body),
    }
    return {"idle_video": idle_path, "body_video": body_path,
            "face_path": face_path, "meta": meta}


if __name__ == "__main__":
    import sys, json
    src = sys.argv[1] if len(sys.argv) > 1 else "IMG_8584.MOV"
    name = sys.argv[2] if len(sys.argv) > 2 else "test"
    out = sys.argv[3] if len(sys.argv) > 3 else "avatar_videos"
    r = build_avatar_assets(src, out, name)
    print(json.dumps(r["meta"], ensure_ascii=False, indent=2))
    print("idle:", r["idle_video"])
    print("body:", r["body_video"])
    print("face:", r["face_path"])
