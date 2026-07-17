# coding: utf-8
"""
LivePortrait wrapper: turn a single still portrait into a sequence of "alive" frames
(head sway + blinking + subtle expression) by driving it with a pre-made motion template.

Designed to run inside the lipsync (musethepeak) GPU worker thread, alongside MuseTalk.
Returns full-resolution BGR frames so MuseTalk can lip-sync directly on top of them.

Key fixes for this environment:
  * Workspace path contains non-ASCII (Chinese) chars -> cv2.imread() of the paste-back
    mask returns None. We reload the mask via np.fromfile + cv2.imdecode.
  * No system ffmpeg needed: we never encode video here, just return numpy frames.
"""
import os
import os.path as osp
import sys
import threading

import cv2
import numpy as np
import torch

import app_config
_LP_DIR = str(app_config.BASE / "LivePortrait")
_DEFAULT_TEMPLATE = osp.join(_LP_DIR, "idle_templates", "idle.pkl")

# 神态阻尼：让 idle 更中性自然(默认驱动偏笑/头动大)。
#   EXP_SCALE 降整体表情幅度(微笑/眉)；EYE_SCALE 单独保留眼部关键点(眨眼要完整,否则像眯眼)；
#   HEAD_T/R_SCALE 适度收敛头部平移/旋转幅度。
_EXP_SCALE = float(os.environ.get("ALIVE_EXP_SCALE", "0.55"))
_EYE_SCALE = float(os.environ.get("ALIVE_EYE_SCALE", "1.0"))
_HEAD_T_SCALE = float(os.environ.get("ALIVE_HEAD_T_SCALE", "0.7"))
_HEAD_R_SCALE = float(os.environ.get("ALIVE_HEAD_R_SCALE", "0.7"))
_EYE_IDX = (11, 13, 15, 16, 18)  # LivePortrait 21 关键点中的眼部索引

_pipe = None            # LivePortraitPipeline (lazy, GPU-thread bound)
_lp = None              # live_portrait_wrapper
_cropper = None
_inf_cfg = None
_load_lock = threading.Lock()


def _ensure_pipeline():
    """Lazy-load LivePortrait models. Must be called from the GPU worker thread."""
    global _pipe, _lp, _cropper, _inf_cfg
    if _pipe is not None:
        return _pipe
    with _load_lock:
        if _pipe is not None:
            return _pipe
        if _LP_DIR not in sys.path:
            sys.path.insert(0, _LP_DIR)
        from src.config.argument_config import ArgumentConfig
        from src.config.inference_config import InferenceConfig
        from src.config.crop_config import CropConfig
        from src.live_portrait_pipeline import LivePortraitPipeline

        def _partial(cls, kw):
            return cls(**{k: v for k, v in kw.items() if hasattr(cls, k)})

        args = ArgumentConfig(source="", driving="", output_dir="")
        inf_cfg = _partial(InferenceConfig, args.__dict__)
        crop_cfg = _partial(CropConfig, args.__dict__)
        # cv2.imread fails on the Chinese workspace path -> reload mask via imdecode
        _mask_p = osp.join(_LP_DIR, "src", "utils", "resources", "mask_template.png")
        try:
            inf_cfg.mask_crop = cv2.imdecode(np.fromfile(_mask_p, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            pass
        pipe = LivePortraitPipeline(inference_cfg=inf_cfg, crop_cfg=crop_cfg)
        _pipe, _lp, _cropper, _inf_cfg = pipe, pipe.live_portrait_wrapper, pipe.cropper, inf_cfg
        return _pipe


def build_lookaway_motion(n_frames: int = 24, max_yaw_deg: float = 16.0,
                          max_pitch_deg: float = 4.0):
    """构造一段「看向别处再回来」的程序化驱动 motion（纯头部偏转，表情中性）。

    不依赖任何驱动视频/模板：只生成每帧的头部旋转矩阵 R（yaw 为主、轻微 pitch），
    exp/t/scale 全帧相同→相对首帧的增量为 0→表情/位移中性，仅头转。配合
    generate_alive_frames(motion=...) 使用，得到「偶尔瞥向一侧」的待机变体帧。

    用半周期正弦让头转出去再回到正面，首尾都≈正脸，便于和主待机循环无缝拼接。
    """
    if _LP_DIR not in sys.path:
        sys.path.insert(0, _LP_DIR)
    from src.utils.camera import get_rotation_matrix
    n = max(2, int(n_frames))
    exp0 = torch.zeros(1, 21, 3, dtype=torch.float32)
    t0 = torch.zeros(1, 3, dtype=torch.float32)
    scale0 = torch.ones(1, 1, dtype=torch.float32)
    motion = []
    for i in range(n):
        phase = float(np.sin(np.pi * i / (n - 1)))     # 0→1→0，正脸→偏转→正脸
        yaw = float(max_yaw_deg) * phase
        pitch = float(max_pitch_deg) * phase
        R = get_rotation_matrix(torch.tensor([pitch], dtype=torch.float32),
                                torch.tensor([yaw], dtype=torch.float32),
                                torch.tensor([0.0], dtype=torch.float32))    # (1,3,3)
        motion.append({"R": R.float(), "exp": exp0.clone(), "t": t0.clone(),
                       "scale": scale0.clone()})
    return motion


@torch.no_grad()
def generate_alive_frames(face_img_bgr, template_path=None, max_frames=None, motion=None):
    """Animate a still portrait into 'alive' frames using a motion template.

    Args:
        face_img_bgr: HxWx3 BGR uint8 numpy array (the portrait).
        template_path: path to a LivePortrait motion template (.pkl). Defaults to idle.pkl.
        max_frames: optionally cap number of frames.
        motion: 可选，直接给定每帧 motion 字典列表（程序化驱动，见 build_lookaway_motion）；
                给定时忽略 template_path/pkl 加载。

    Returns:
        list of full-resolution BGR uint8 frames (head/eye/expression animated).
        Returns [] if no face detected or template missing.
    """
    pipe = _ensure_pipeline()   # also inserts LivePortrait dir into sys.path
    from src.utils.camera import get_rotation_matrix
    from src.utils.crop import prepare_paste_back, paste_back
    from src.utils.io import load, resize_to_limit
    from src.utils.helper import dct2device

    lp = _lp
    cropper = _cropper
    inf_cfg = _inf_cfg
    device = lp.device

    if motion is not None:
        n_frames = len(motion)
        if max_frames:
            n_frames = min(n_frames, int(max_frames))
    else:
        tpl = template_path or _DEFAULT_TEMPLATE
        if not osp.exists(tpl):
            return []
        driving_template_dct = load(tpl)
        motion = driving_template_dct["motion"]
        n_frames = driving_template_dct.get("n_frames", len(motion))
        if max_frames:
            n_frames = min(n_frames, int(max_frames))

    # ---- source (image) preparation ----
    img_rgb = cv2.cvtColor(face_img_bgr, cv2.COLOR_BGR2RGB)
    img_rgb = resize_to_limit(img_rgb, inf_cfg.source_max_dim, inf_cfg.source_division)

    crop_info = cropper.crop_source_image(img_rgb, cropper.crop_cfg)
    if crop_info is None:
        return []
    source_lmk = crop_info["lmk_crop"]
    img_crop_256 = crop_info["img_crop_256x256"]

    I_s = lp.prepare_source(img_crop_256)
    x_s_info = lp.get_kp_info(I_s)
    x_c_s = x_s_info["kp"]
    R_s = get_rotation_matrix(x_s_info["pitch"], x_s_info["yaw"], x_s_info["roll"])
    f_s = lp.extract_feature_3d(I_s)
    x_s = lp.transform_keypoint(x_s_info)

    # neutralize lip-open at the source so the mouth starts closed (MuseTalk drives the mouth)
    lip_delta_before = None
    if inf_cfg.flag_normalize_lip and inf_cfg.flag_relative_motion and source_lmk is not None:
        c_d_lip_before = [0.0]
        comb = lp.calc_combined_lip_ratio(c_d_lip_before, source_lmk)
        if comb[0][0] >= inf_cfg.lip_normalize_threshold:
            lip_delta_before = lp.retarget_lip(x_s, comb)

    mask_ori_float = prepare_paste_back(
        inf_cfg.mask_crop, crop_info["M_c2o"],
        dsize=(img_rgb.shape[1], img_rgb.shape[0]),
    )

    out_frames = []
    R_d_0, x_d_0_info = None, None
    for i in range(n_frames):
        x_d_i_info = dct2device(motion[i], device)
        R_d_i = x_d_i_info["R"] if "R" in x_d_i_info else x_d_i_info["R_d"]
        if i == 0:
            R_d_0 = R_d_i
            x_d_0_info = x_d_i_info.copy()

        # relative motion, animation_region == "all"，叠加神态阻尼
        R_rel = R_d_i @ R_d_0.permute(0, 2, 1)            # 相对首帧的头部旋转
        if _HEAD_R_SCALE != 1.0:                           # 用 Rodrigues 缩放旋转角度
            _rm = R_rel[0].detach().cpu().numpy().astype(np.float64)
            _rv, _ = cv2.Rodrigues(_rm)
            _rm2, _ = cv2.Rodrigues(_rv * _HEAD_R_SCALE)
            R_rel = torch.from_numpy(_rm2).to(R_d_i)[None]
        R_new = R_rel @ R_s

        _exp_delta = x_d_i_info["exp"] - x_d_0_info["exp"]
        _delta_scaled = _exp_delta * _EXP_SCALE
        if _EYE_SCALE != _EXP_SCALE:                       # 眼部单独缩放，保住完整眨眼
            for _ei in _EYE_IDX:
                _delta_scaled[:, _ei, :] = _exp_delta[:, _ei, :] * _EYE_SCALE
        delta_new = x_s_info["exp"] + _delta_scaled

        scale_new = x_s_info["scale"] * (x_d_i_info["scale"] / x_d_0_info["scale"])
        t_new = x_s_info["t"] + (x_d_i_info["t"] - x_d_0_info["t"]) * _HEAD_T_SCALE
        t_new[..., 2].fill_(0)

        x_d_i_new = scale_new * (x_c_s @ R_new + delta_new) + t_new

        if inf_cfg.flag_stitching:
            x_d_i_new = lp.stitching(x_s, x_d_i_new)
            if lip_delta_before is not None:
                x_d_i_new = x_d_i_new + lip_delta_before

        out = lp.warp_decode(f_s, x_s, x_d_i_new)
        I_p_i = lp.parse_output(out["out"])[0]  # RGB full crop -> paste back
        I_pstbk = paste_back(I_p_i, crop_info["M_c2o"], img_rgb, mask_ori_float)
        out_frames.append(cv2.cvtColor(I_pstbk, cv2.COLOR_RGB2BGR))

    return out_frames
