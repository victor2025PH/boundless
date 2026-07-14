# -*- coding: utf-8 -*-
"""阶段14 PoC：CatV2TON 在 fitdit 环境(diffusers 0.38/transformers 5.x/setuptools 82)
下的运行时垫片。零仓库修改、零环境降级——import 本模块即完成注入。

漂移点与对策：
  ① setuptools>=81 移除 pkg_resources → 用 packaging.version 造等价 parse_version。
  ② transformers 5.x 移除 MT5Tokenizer → easyanimate 多文本编码器管线 import 即炸；
     但 modules/pipeline.py 只从它拿 get_2d_rotary_pos_embed / get_resize_crop_region_for_grid
     两个纯几何函数，且 diffusers 0.38 本体就有 → 预注册 stub 模块直供。
  ③ detectron2/densepose/fvcore 不装 → PoC 用 load_pose=False（管线原生支持），
     mask 改用自有 FitDiT 人体解析。但 modules/__init__.py 顶层就 import
     AutoMasker（→densepose→detectron2→fvcore）→ 注册空壳包骨架跳过 __init__，
     子模块 modules.pipeline 照常按 __path__ 加载。"""
import importlib.util
import sys
import types


def install():
    # ① pkg_resources 垫片
    if "pkg_resources" not in sys.modules:
        try:
            import pkg_resources  # noqa: F401  真身还在就用真身
        except ImportError:
            from packaging.version import Version

            m = types.ModuleType("pkg_resources")
            m.parse_version = Version
            sys.modules["pkg_resources"] = m

    # ② 多文本编码器管线 stub（必须抢在 easyanimate 真模块 import 前注册）。
    #    重要：不能直接用 0.38 的 get_2d_rotary_pos_embed——它把网格从 numpy
    #    linspace(endpoint=False) 换成了 torch.linspace(含端点)，cos/sin 相对
    #    0.29 训练期漂移高达 0.16 → 位置编码整体错位，实测衣区直接出纯噪声。
    #    这里按 0.29 原版逐行复刻（训练-推理位置编码必须逐位一致）。
    name = "easyanimate.pipeline.pipeline_easyanimate_multi_text_encoder"
    if name not in sys.modules:
        import numpy as _np
        import torch as _torch
        from diffusers.pipelines.hunyuandit.pipeline_hunyuandit import (
            get_resize_crop_region_for_grid)

        def _rope1d_029(dim, pos):
            freqs = 1.0 / (10000 ** (_torch.arange(0, dim, 2)[: dim // 2].float() / dim))
            freqs = _torch.outer(_torch.from_numpy(pos), freqs).float()
            return (freqs.cos().repeat_interleave(2, dim=1),
                    freqs.sin().repeat_interleave(2, dim=1))

        def _rope2d_029(embed_dim, crops_coords, grid_size, use_real=True):
            start, stop = crops_coords
            gh = _np.linspace(start[0], stop[0], grid_size[0], endpoint=False, dtype=_np.float32)
            gw = _np.linspace(start[1], stop[1], grid_size[1], endpoint=False, dtype=_np.float32)
            grid = _np.stack(_np.meshgrid(gw, gh), axis=0)     # w 在前，同 0.29
            emb_a = _rope1d_029(embed_dim // 2, grid[0].reshape(-1))   # 0.29 逐位一致：
            emb_b = _rope1d_029(embed_dim // 2, grid[1].reshape(-1))   # 先 grid[0] 后 grid[1]
            return (_torch.cat([emb_a[0], emb_b[0]], dim=1),
                    _torch.cat([emb_a[1], emb_b[1]], dim=1))

        stub = types.ModuleType(name)
        stub.get_2d_rotary_pos_embed = _rope2d_029
        stub.get_resize_crop_region_for_grid = get_resize_crop_region_for_grid
        sys.modules[name] = stub

    # ②b torchvision 0.25+ 移除 io.read_video/write_video（旧视频 API 下线），
    #    data/utils.py 顶层 import 即炸。补 cv2 实现（PoC 自有 runner 只在
    #    保存结果时可能用到 write_video，read_video 按 TCHW uint8 语义等价实现）。
    import torchvision.io as _tvio
    if not hasattr(_tvio, "read_video"):
        import cv2
        import numpy as np
        import torch

        def _cv2_read_video(path, pts_unit="sec", output_format="THWC", **_kw):
            cap = cv2.VideoCapture(str(path))
            frames = []
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            cap.release()
            v = torch.from_numpy(np.stack(frames)) if frames else torch.zeros(0, 1, 1, 3, dtype=torch.uint8)
            if output_format == "TCHW":
                v = v.permute(0, 3, 1, 2)
            return v, torch.zeros(0), {"video_fps": fps}

        def _cv2_write_video(path, video_array, fps, **_kw):
            arr = video_array.cpu().numpy() if hasattr(video_array, "cpu") else np.asarray(video_array)
            arr = arr.astype(np.uint8)                     # THWC, RGB
            h, w = arr.shape[1:3]
            vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (w, h))
            for f in arr:
                vw.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
            vw.release()

        _tvio.read_video = _cv2_read_video
        _tvio.write_video = _cv2_write_video

    # ③ modules 包骨架：跳过其 __init__.py（顶层 import AutoMasker→detectron2 链），
    #    保留 __path__ 让 `modules.pipeline` 等子模块正常按路径加载。
    if "modules" not in sys.modules:
        pkg = types.ModuleType("modules")
        pkg.__path__ = [r"C:\CatV2TON\modules"]
        pkg.__package__ = "modules"
        pkg.__spec__ = importlib.util.spec_from_loader("modules", loader=None,
                                                       is_package=True)
        sys.modules["modules"] = pkg


install()
