# -*- coding: utf-8 -*-
"""bg_images 上传端点验证（无需起 hub）：AST 抽出 avatar_hub 的
_bg_image_safe_name / api_bg_images / api_bg_images_upload（连同 _BG_* 常量），
挂到临时 FastAPI app + 临时目录，用 TestClient 全链路测：正常上传 / 重名加序号 /
穿越清洗 / 坏图坏后缀坏视频拒绝 / 空文件拒绝 / 视频与 GIF 上传。"""
import ast
import io
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

BASE = Path(r"C:\模仿音色")
sys.path.insert(0, str(BASE))

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.testclient import TestClient
from PIL import Image

src = (BASE / "avatar_hub.py").read_text(encoding="utf-8")
tree = ast.parse(src)
want = {"_bg_image_safe_name", "api_bg_images", "api_bg_images_upload"}
want_consts = {"_BG_IMG_EXTS", "_BG_VID_EXTS", "_BG_IMG_MAX_MB", "_BG_VID_MAX_MB"}
nodes = []
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in want:
        nodes.append(n)
    elif isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in want_consts for t in n.targets):
        nodes.append(n)
got = {n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
assert got == want, f"avatar_hub.py 缺函数: {want - got}"

app = FastAPI()
tmpd = tempfile.mkdtemp(prefix="bgup_")
ns = {"app": app, "os": os, "re": re, "io": io, "Path": Path, "uuid": uuid,
      "HTTPException": HTTPException, "UploadFile": UploadFile, "File": File,
      "_BASE": tmpd}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "hub.bg_upload", "exec"), ns)

client = TestClient(app)
bg_dir = Path(tmpd) / "bg_images"
v = {}


def png_bytes():
    b = io.BytesIO()
    Image.new("RGB", (64, 36), (30, 160, 220)).save(b, "PNG")
    return b.getvalue()


# 1) 正常上传（含中文/空格文件名）→ 落盘 + 出现在清单 + 自动建目录
r = client.post("/api/bg_images/upload",
                files={"file": ("我的 背景.png", png_bytes(), "image/png")})
j = r.json()
v["正常上传(中文名)"] = (r.status_code == 200 and j.get("ok") is True
                         and j.get("saved") == "我的 背景.png"
                         and "我的 背景.png" in j.get("images", [])
                         and (bg_dir / "我的 背景.png").exists())
print(f"[1] 上传: {r.status_code} saved={j.get('saved')} images={j.get('images')}")

# 2) 同名再传 → 自动加序号不覆盖
r = client.post("/api/bg_images/upload",
                files={"file": ("我的 背景.png", png_bytes(), "image/png")})
j = r.json()
v["重名自动加序号"] = j.get("saved") == "我的 背景_2.png" and (bg_dir / "我的 背景_2.png").exists()
print(f"[2] 重名: saved={j.get('saved')}")

# 3) 路径穿越文件名 → 只取 basename，文件落在 bg_images 内
r = client.post("/api/bg_images/upload",
                files={"file": (r"..\..\evil.png", png_bytes(), "image/png")})
j = r.json()
v["穿越文件名清洗"] = (j.get("saved") == "evil.png" and (bg_dir / "evil.png").exists()
                       and not (Path(tmpd).parent / "evil.png").exists())
print(f"[3] 穿越: saved={j.get('saved')}")

# 4) Windows 非法字符 → 替换为 _（引号经 multipart 会被转义，纯函数直测）；超长主干截 60
safe = ns["_bg_image_safe_name"]
v["非法字符替换"] = (safe('a<b>:c"d|e?.PNG') == ("a_b__c_d_e_", ".png")
                     and safe("x.gif") == ("x", ".gif")      # 动图/视频后缀现已放行
                     and safe("x.mp4") == ("x", ".mp4")
                     and safe("x.exe") is None and safe(r"..\y\z.png") == ("z", ".png")
                     and safe("...png") is None       # 纯点文件名视作无后缀 → 拒
                     and safe(". .png") == ("bg", ".png"))   # 主干清洗成空 → 兜底名
r = client.post("/api/bg_images/upload",
                files={"file": ("长" * 80 + ".png", png_bytes(), "image/png")})
v["超长截断"] = r.json().get("saved") == "长" * 60 + ".png"
print(f"[4] 清洗: 非法字符={v['非法字符替换']} 截长={v['超长截断']}")

# 5) 拒绝项：坏后缀 / 假图(解码失败) / 空文件 / 图片超 15MB / 假视频(解码失败)
r1 = client.post("/api/bg_images/upload", files={"file": ("x.exe", png_bytes(), "application/x-msdownload")})
r2 = client.post("/api/bg_images/upload", files={"file": ("x.jpg", b"not an image", "image/jpeg")})
r3 = client.post("/api/bg_images/upload", files={"file": ("x.jpg", b"", "image/jpeg")})
r4 = client.post("/api/bg_images/upload",
                 files={"file": ("x.jpg", b"\xff" * (15 * 1024 * 1024 + 1), "image/jpeg")})
r5 = client.post("/api/bg_images/upload",
                 files={"file": ("假视频.mp4", b"definitely not video" * 50, "video/mp4")})
v["越界整单拒绝"] = all(r.status_code == 400 for r in (r1, r2, r3, r4, r5))
v["拒绝后无残留临时文件"] = not list(bg_dir.glob("*.part"))
print(f"[5] 拒绝: 坏后缀={r1.status_code} 假图={r2.status_code} 空={r3.status_code} "
      f"超大={r4.status_code} 假视频={r5.status_code}")

# 6) 真视频与 GIF 上传 → 200 且落盘
import numpy as np
import cv2
vid_tmp = os.path.join(tmpd, "t.mp4")
vw = cv2.VideoWriter(vid_tmp, cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 36))
for i in range(10):
    vw.write(np.full((36, 64, 3), i * 20, np.uint8))
vw.release()
r = client.post("/api/bg_images/upload",
                files={"file": ("海浪 循环.mp4", open(vid_tmp, "rb").read(), "video/mp4")})
j = r.json()
v["视频上传"] = (r.status_code == 200 and j.get("saved") == "海浪 循环.mp4"
                 and (bg_dir / "海浪 循环.mp4").exists())
gif_b = io.BytesIO()
frames = [Image.new("RGB", (64, 36), (i * 20, 100, 200 - i * 15)) for i in range(8)]
frames[0].save(gif_b, "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
r = client.post("/api/bg_images/upload",
                files={"file": ("动图.gif", gif_b.getvalue(), "image/gif")})
j2 = r.json()
v["GIF上传"] = r.status_code == 200 and j2.get("saved") == "动图.gif"
print(f"[6] 视频={v['视频上传']} gif={v['GIF上传']} note={j.get('note') or '(无)'}")

# 7) GET 清单口与上传结果一致（图+视频都在）
imgs = client.get("/api/bg_images").json().get("images", [])
v["清单回读"] = ("我的 背景.png" in imgs and "evil.png" in imgs
                 and "海浪 循环.mp4" in imgs and "动图.gif" in imgs)
print(f"[7] 清单: {len(imgs)} 项")

fails = [k for k, okv in v.items() if not okv]
for k, okv in v.items():
    print(("[OK] " if okv else "[NG] ") + k)
print("RESULT: " + ("ALL PASS" if not fails else f"FAIL {fails}"))
sys.exit(0 if not fails else 1)
