# -*- coding: utf-8 -*-
"""DFM 角色库实验室（离线·非实时，端口 8005）
================================================
一个独立服务，把社区下载的 DFM 角色模型做成"画廊 + 一键试换"的测试/展示台：
  · 画廊：读 dfm_workspace/dfm_registry.json，按分类展示中文名 + 换脸缩略图 + 辨识度指标；
  · 试换：用户上传一张照片(或选内置样例) → 选一个角色 → 现场换脸 → 出「原图|换脸」对比；
  · 合规闸口：政治人物(blocked)默认不进画廊；输出统一打「AI 合成」水印。

为什么独立成服务（不动生产 8000）：一个 faceswap 进程只能常驻一个模型，画廊要随点随换
124 个角色，只能按需加载。本服务用 LRU 缓存最近用的几个 DFM，默认 CPU 推理(单次~1-2s)，
不抢生产 GPU；与发型(8001)/试衣(8002)/妆容(8004) 同为"lab 离线服务"范式，Hub 用同一套
/api/lab/services 卡片挂入口。
"""
import os, io, sys, json, base64, time, threading
from collections import OrderedDict
from pathlib import Path
import numpy as np, cv2

BASE = Path(r"C:\模仿音色")
REGISTRY = BASE / "dfm_workspace" / "dfm_registry.json"
THUMBS = BASE / "dfm_workspace" / "_community_thumbs"
SAMPLES = BASE / "dfm_workspace" / "_pilot_dst_faces"
PORT = int(os.environ.get("DFM_LAB_PORT", "8005"))
USE_GPU = os.environ.get("DFM_LAB_GPU", "0") == "1"

# ── 复用生产同款 DFL 对齐 + LAB 色迁移（与 faceswap_api 逐行一致，保证 lab 所见=上线所得）──
_DFL_L2D_NEW = np.array([
    [0.000213256,0.106454],[0.0752622,0.038915],[0.18113,0.0187482],[0.29077,0.0344891],[0.393397,0.0773906],
    [0.586856,0.0773906],[0.689483,0.0344891],[0.799124,0.0187482],[0.904991,0.038915],[0.98004,0.106454],
    [0.490127,0.203352],[0.490127,0.307009],[0.490127,0.409805],[0.490127,0.515625],
    [0.36688,0.587326],[0.426036,0.609345],[0.490127,0.628106],[0.554217,0.609345],[0.613373,0.587326],
    [0.121737,0.216423],[0.187122,0.178758],[0.265825,0.179852],[0.334606,0.231733],[0.260918,0.245099],[0.182743,0.244077],
    [0.645647,0.231733],[0.714428,0.179852],[0.793132,0.178758],[0.858516,0.216423],[0.79751,0.244077],[0.719335,0.245099],
    [0.254149,0.780233],[0.726104,0.780233]], dtype=np.float32)


def _umeyama(src, dst, estimate_scale=True):
    num, dim = src.shape
    sm, dm = src.mean(0), dst.mean(0)
    sd, dd = src - sm, dst - dm
    A = dd.T @ sd / num
    d = np.ones((dim,))
    if np.linalg.det(A) < 0:
        d[dim - 1] = -1
    T = np.eye(dim + 1)
    U, S, V = np.linalg.svd(A)
    r = np.linalg.matrix_rank(A)
    if r == dim - 1:
        if np.linalg.det(U) * np.linalg.det(V) > 0:
            T[:dim, :dim] = U @ V
        else:
            s = d[dim - 1]; d[dim - 1] = -1; T[:dim, :dim] = U @ np.diag(d) @ V; d[dim - 1] = s
    else:
        T[:dim, :dim] = U @ np.diag(d) @ V
    scale = 1.0 / sd.var(0).sum() * (S @ d) if estimate_scale else 1.0
    T[:dim, dim] = dm - scale * (T[:dim, :dim] @ sm)
    T[:dim, :dim] *= scale
    return T


def _wf_mat(lm68, S, pad=0.40, fore=0.07):
    sub = np.concatenate([lm68[17:49], lm68[54:55]]).astype(np.float32)
    mat = _umeyama(sub, _DFL_L2D_NEW, True)[0:2].astype(np.float32)
    def _t(pts):
        p = np.expand_dims(np.asarray(pts, dtype=np.float32), 1)
        return np.squeeze(cv2.transform(p, cv2.invertAffineTransform(mat), p.shape))
    g = _t([(0,0),(1,0),(1,1),(0,1),(0.5,0.5)]); gc = g[4].astype(np.float32)
    tb = (g[2]-g[0]).astype(np.float32); tb /= (np.linalg.norm(tb)+1e-8)
    bt = (g[1]-g[3]).astype(np.float32); bt /= (np.linalg.norm(bt)+1e-8)
    mod = np.linalg.norm(g[0]-g[2]) * (pad*np.sqrt(2.0)+0.5)
    vec = (g[0]-g[3]).astype(np.float32); vl = np.linalg.norm(vec)+1e-8; vec /= vl; gc = gc + vec*vl*fore
    l_t = np.array([gc-tb*mod, gc+bt*mod, gc+tb*mod], dtype=np.float32)
    return cv2.getAffineTransform(l_t, np.float32([(0,0),(S,0),(S,S)]))


def _lab_color_transfer(src, ref, mask):
    m = mask > 0.5
    if m.sum() < 50:
        return src
    s = cv2.cvtColor(src, cv2.COLOR_BGR2LAB).astype(np.float32)
    r = cv2.cvtColor(ref, cv2.COLOR_BGR2LAB).astype(np.float32)
    out = s.copy()
    for c in range(3):
        ss = s[:, :, c][m].std() + 1e-6; sm = s[:, :, c][m].mean()
        rs = r[:, :, c][m].std() + 1e-6; rm = r[:, :, c][m].mean()
        out[:, :, c] = (s[:, :, c] - sm) / ss * rs + rm
    return cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


# ── 按需加载 + LRU 缓存 DFM 会话 ──────────────────────────────
_PROV = (["CUDAExecutionProvider", "CPUExecutionProvider"] if USE_GPU else ["CPUExecutionProvider"])
_lru = OrderedDict()
_lru_cap = 4
_lru_lock = threading.Lock()
_fa = None
_fa_lock = threading.Lock()


def _face_analyser():
    global _fa
    if _fa is None:
        with _fa_lock:
            if _fa is None:
                from insightface.app import FaceAnalysis
                fa = FaceAnalysis(name="buffalo_l", providers=_PROV)
                fa.prepare(ctx_id=(0 if USE_GPU else -1), det_size=(640, 640))
                _fa = fa
    return _fa


def _get_session(path: Path):
    key = str(path)
    with _lru_lock:
        if key in _lru:
            _lru.move_to_end(key)
            return _lru[key]
    import onnxruntime as ort
    sess = ort.InferenceSession(str(path), providers=_PROV)
    ins = sess.get_inputs()
    info = {"sess": sess, "W": int(ins[0].shape[2]), "morph": len(ins) == 2,
            "onames": [o.name for o in sess.get_outputs()]}
    with _lru_lock:
        _lru[key] = info; _lru.move_to_end(key)
        while len(_lru) > _lru_cap:
            _lru.popitem(last=False)
    return info


def _dfm_swap(img, model_path: Path, morph=0.75):
    fa = _face_analyser()
    faces = fa.get(img)
    if not faces:
        return None, "未检测到人脸"
    tf = max(faces, key=lambda x: (x.bbox[2]-x.bbox[0])*(x.bbox[3]-x.bbox[1]))
    lm = getattr(tf, "landmark_3d_68", None)
    if lm is None:
        return None, "检测器未给出 landmark"
    info = _get_session(model_path)
    S = info["W"]
    M = _wf_mat(np.asarray(lm[:, :2], dtype=np.float32), S)
    aimg = cv2.warpAffine(img, M, (S, S), flags=cv2.INTER_CUBIC)
    blob = np.expand_dims(aimg.astype(np.float32) / 255.0, 0)
    feed = {"in_face:0": blob}
    if info["morph"]:
        feed["morph_value:0"] = np.float32([morph])
    outs = info["sess"].run(None, feed)
    omap = {n: o[0] for n, o in zip(info["onames"], outs)}
    celeb = (np.clip(omap["out_celeb_face:0"], 0, 1) * 255).astype(np.uint8)
    mask = np.clip(omap["out_celeb_face_mask:0"] * omap["out_face_mask:0"], 0, 1)[:, :, 0]
    celeb = _lab_color_transfer(celeb, aimg, mask)
    IM = cv2.invertAffineTransform(M)
    h, w = img.shape[:2]
    back = cv2.warpAffine(celeb, IM, (w, h), flags=cv2.INTER_CUBIC)
    mb = cv2.warpAffine(mask, IM, (w, h), flags=cv2.INTER_CUBIC)
    mb = cv2.GaussianBlur(mb, (0, 0), 3)[..., None]
    out = (back.astype(np.float32) * mb + img.astype(np.float32) * (1 - mb)).astype(np.uint8)
    return out, None


def _watermark(img):
    """右下角「AI 合成」水印——深伪合规最低要求，lab 输出一律带。"""
    h, w = img.shape[:2]
    txt = "AI synthetic"
    fs = max(0.4, w / 900)
    (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
    x, y = w - tw - 10, h - 10
    cv2.putText(img, txt, (x+1, y+1), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), 2, cv2.LINE_AA)
    cv2.putText(img, txt, (x, y), cv2.FONT_HERSHEY_SIMPLEX, fs, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def _imread_u(p):
    """Unicode 安全读图：cv2.imread 在中文路径(C:\模仿音色)返回 None，必须走 np.fromfile。"""
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def _b64_to_img(b64):
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    return cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_COLOR)


def _img_to_b64(img, q=90):
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return "data:image/jpeg;base64," + base64.b64encode(buf).decode()


# ── FastAPI ──────────────────────────────────────────────────
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

app = FastAPI(title="DFM 角色库 Lab")


def _load_registry(include_blocked=False):
    if not REGISTRY.exists():
        return []
    rj = json.loads(REGISTRY.read_text(encoding="utf-8"))
    es = rj.get("entries", [])
    out = []
    for e in es:
        if not (BASE / e["path"]).exists():      # 只列真的下载好的
            continue
        if e["compliance"] == "blocked" and not include_blocked:
            continue
        out.append(e)
    return out


@app.get("/health")
def health():
    reg = _load_registry(include_blocked=True)
    return {"status": "ok", "service": "dfm-lab", "port": PORT,
            "characters": len(reg), "gpu": USE_GPU}


@app.get("/api/characters")
def characters():
    reg = _load_registry()
    reg.sort(key=lambda e: (e.get("category") or "z", -(e.get("self_id") or 0)))
    return {"n": len(reg), "characters": reg}


@app.get("/thumb")
def thumb(file: str):
    p = THUMBS / (Path(file).stem + ".jpg")
    if not p.exists():
        raise HTTPException(404, "无缩略图")
    return Response(content=p.read_bytes(), media_type="image/jpeg")


@app.get("/api/samples")
def samples():
    files = sorted(SAMPLES.glob("*.jpg"))[:12]
    out = []
    for p in files:
        im = _imread_u(p)
        if im is not None:
            out.append({"id": p.name, "img": _img_to_b64(im, 80)})
    return {"samples": out}


@app.post("/try")
def try_swap(data: dict):
    file = data.get("character", "")
    reg = {e["file"]: e for e in _load_registry(include_blocked=True)}
    if file not in reg:
        raise HTTPException(404, f"未知角色: {file}")
    ent = reg[file]
    if ent["compliance"] == "blocked":
        raise HTTPException(403, "该角色为合规下架项，不可试换")
    if data.get("sample"):
        sp = SAMPLES / data["sample"]
        if not sp.exists():
            raise HTTPException(404, "样例不存在")
        img = _imread_u(sp)
    elif data.get("image"):
        img = _b64_to_img(data["image"])
    else:
        raise HTTPException(400, "需要 image 或 sample")
    if img is None:
        raise HTTPException(400, "图片解码失败")
    t0 = time.time()
    out, err = _dfm_swap(img, BASE / ent["path"], morph=float(data.get("morph", 0.75)))
    if err:
        raise HTTPException(422, err)
    out = _watermark(out)
    return {"ok": True, "cn": ent["cn"], "ms": int((time.time()-t0)*1000),
            "before": _img_to_b64(img), "after": _img_to_b64(out)}


@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return HTMLResponse(_UI_HTML)


_UI_HTML = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DFM 角色库 · 换脸实验室</title>
<style>
 *{box-sizing:border-box}
 body{font-family:"Segoe UI",Arial,sans-serif;background:#0f1020;color:#eee;margin:0;padding:0}
 header{background:linear-gradient(135deg,#1a1a3e,#2d1b4e);padding:18px 24px;position:sticky;top:0;z-index:10;box-shadow:0 2px 12px rgba(0,0,0,.4)}
 h1{margin:0;font-size:20px;color:#fff}
 .sub{color:#9aa;font-size:12px;margin-top:4px}
 .badge{background:#3d2e00;color:#fbbf24;padding:2px 8px;border-radius:999px;font-size:11px;margin-left:8px}
 .wrap{display:flex;gap:16px;padding:16px 24px;flex-wrap:wrap}
 .left{flex:1;min-width:320px}
 .right{width:420px;max-width:100%}
 .filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
 .chip{background:#1b1c33;border:1px solid #333;color:#ccc;padding:6px 12px;border-radius:999px;cursor:pointer;font-size:13px}
 .chip.on{background:#e94560;border-color:#e94560;color:#fff}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px}
 .card{background:#16172e;border-radius:10px;overflow:hidden;cursor:pointer;border:2px solid transparent;transition:.15s}
 .card:hover{border-color:#e94560;transform:translateY(-2px)}
 .card.sel{border-color:#4caf50}
 .card img{width:100%;height:150px;object-fit:cover;object-position:top;display:block;background:#000}
 .card .info{padding:8px}
 .card .nm{font-size:13px;font-weight:bold;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .card .mt{font-size:11px;color:#9aa;margin-top:3px}
 .tag{display:inline-block;font-size:10px;padding:1px 6px;border-radius:4px;margin-top:4px}
 .t-hollywood{background:#1e3a5f;color:#7ec8ff}.t-asian{background:#5f1e3a;color:#ff9ecb}
 .t-music{background:#3a5f1e;color:#bfff7e}.t-character{background:#4a1e5f;color:#d89eff}
 .t-other{background:#333;color:#aaa}.t-influencer{background:#5f3a1e;color:#ffcb7e}
 .t-live{background:#0d3b0d;color:#4caf50;margin-left:4px}
 .panel{background:#16172e;border-radius:12px;padding:16px;position:sticky;top:90px}
 .panel h3{margin:0 0 12px;color:#e94560;font-size:15px}
 .drop{border:2px dashed #444;border-radius:8px;padding:18px;text-align:center;cursor:pointer;font-size:13px;color:#9aa}
 .drop:hover{border-color:#e94560;background:#1b1c33}
 .samples{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
 .samples img{width:52px;height:52px;object-fit:cover;border-radius:6px;cursor:pointer;border:2px solid transparent}
 .samples img:hover,.samples img.on{border-color:#e94560}
 .cmp{display:flex;gap:8px;margin-top:12px}
 .cmp div{flex:1;text-align:center;font-size:12px;color:#9aa}
 .cmp img{width:100%;border-radius:8px;background:#000;min-height:120px}
 .btn{background:#e94560;color:#fff;border:none;padding:11px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:bold;width:100%;margin-top:12px}
 .btn:disabled{background:#555;cursor:not-allowed}
 .btn:hover:not(:disabled){background:#c73652}
 .hint{font-size:12px;color:#9aa;margin-top:8px;text-align:center;min-height:16px}
 .morph{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:12px;color:#9aa}
 .morph input{flex:1;accent-color:#e94560}
</style></head><body>
<header>
 <h1>🎭 DFM 角色库 · 换脸实验室 <span class="badge">🧪 离线 · 非实时</span></h1>
 <div class="sub">整脸换（含骨相/轮廓）· 辨识度最强方案 · 每角色专属模型 · 输出统一带「AI 合成」水印</div>
</header>
<div class="wrap">
 <div class="left">
   <div class="filters" id="filters"></div>
   <div class="grid" id="grid">加载中…</div>
 </div>
 <div class="right">
   <div class="panel">
     <h3>🔬 一键试换</h3>
     <div class="drop" id="drop" onclick="document.getElementById('fi').click()">
       📁 点击上传你的照片（含正脸）<input type="file" id="fi" accept="image/*" style="display:none">
     </div>
     <div style="font-size:12px;color:#9aa;margin-top:8px">或选内置样例脸：</div>
     <div class="samples" id="samples"></div>
     <div class="morph" id="morphBox" style="display:none">
       形态强度 <input type="range" id="morph" min="0" max="1" step="0.05" value="0.75">
       <span id="morphV">0.75</span>
     </div>
     <div class="cmp">
       <div>原图<br><img id="before"></div>
       <div>换脸后<br><img id="after"></div>
     </div>
     <button class="btn" id="go" disabled onclick="run()">选一个角色 + 一张脸</button>
     <div class="hint" id="hint"></div>
   </div>
 </div>
</div>
<script>
let CH=[], sel=null, srcB64=null, srcSample=null, cat='all';
const CN={all:'全部',live:'⚡可直播',hollywood:'欧美影星',asian:'亚洲面孔',music:'音乐人',character:'影视角色',influencer:'网红',other:'其他'};
async function load(){
  const d=await (await fetch('/api/characters')).json(); CH=d.characters;
  const cats=['all','live',...new Set(CH.map(c=>c.category))];
  const nLive=CH.filter(c=>c.live_ok).length;
  document.getElementById('filters').innerHTML=cats.map(c=>`<span class="chip ${c==='all'?'on':''}" data-cat="${c}" onclick="setCat('${c}')">${CN[c]||c}${c==='all'?' ('+CH.length+')':(c==='live'?' ('+nLive+')':'')}</span>`).join('');
  render();
  const s=await (await fetch('/api/samples')).json();
  document.getElementById('samples').innerHTML=s.samples.map(x=>`<img src="${x.img}" title="${x.id}" onclick="pickSample('${x.id}',this)">`).join('');
}
function setCat(c){cat=c;document.querySelectorAll('.chip').forEach(e=>e.classList.toggle('on',e.dataset.cat===c));render();}
function render(){
  const list=CH.filter(c=>cat==='all'||(cat==='live'?c.live_ok:c.category===cat));
  document.getElementById('grid').innerHTML=list.map(c=>{
    const mt=c.self_id!=null?`辨识 ${(c.self_id*100|0)}`:'';
    const lv=c.live_ok?`<span class="tag t-live">⚡直播 ${c.gpu_swap_ms}ms</span>`:(c.gpu_swap_ms?`<span class="tag t-other">🎬仅离线</span>`:'');
    return `<div class="card ${sel&&sel.file===c.file?'sel':''}" onclick='pick(${JSON.stringify(c).replace(/'/g,"&#39;")})'>
      <img src="/thumb?file=${encodeURIComponent(c.file)}" loading="lazy" onerror="this.style.opacity=.2">
      <div class="info"><div class="nm">${c.cn}</div>
      <div class="mt">${c.res||''}px ${mt}${c.morphable?' · 可调形':''}</div>
      <span class="tag t-${c.category}">${CN[c.category]||c.category}</span>${lv}</div></div>`;
  }).join('')||'<div style="color:#9aa">此分类暂无已下载模型</div>';
}
function pick(c){sel=c;render();document.getElementById('morphBox').style.display=c.morphable?'flex':'none';upd();}
function pickSample(id,el){srcSample=id;srcB64=null;document.querySelectorAll('.samples img').forEach(e=>e.classList.remove('on'));el.classList.add('on');document.getElementById('before').src=el.src;upd();}
document.getElementById('fi').onchange=e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=ev=>{srcB64=ev.target.result;srcSample=null;document.querySelectorAll('.samples img').forEach(x=>x.classList.remove('on'));document.getElementById('before').src=srcB64;upd();};r.readAsDataURL(f);};
document.getElementById('morph').oninput=e=>document.getElementById('morphV').textContent=e.target.value;
function upd(){const ok=sel&&(srcB64||srcSample);const b=document.getElementById('go');b.disabled=!ok;b.textContent=ok?('换成 '+sel.cn):'选一个角色 + 一张脸';}
async function run(){
  const b=document.getElementById('go');b.disabled=true;const h=document.getElementById('hint');h.textContent='换脸中…(CPU 首次约 3-5s)';
  const body={character:sel.file,morph:parseFloat(document.getElementById('morph').value)};
  if(srcB64)body.image=srcB64;else body.sample=srcSample;
  try{
    const r=await fetch('/try',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok){h.textContent='⚠ '+(d.detail||'失败');b.disabled=false;return;}
    document.getElementById('before').src=d.before;document.getElementById('after').src=d.after;
    h.textContent=`✅ ${d.cn} · ${d.ms}ms`;
  }catch(e){h.textContent='⚠ '+e;}
  b.disabled=false;
}
load();
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    print("=" * 55)
    print(f" DFM 角色库 Lab 启动  →  http://127.0.0.1:{PORT}/ui")
    print(f" 推理后端: {'GPU' if USE_GPU else 'CPU（默认，不抢生产显存）'}")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
