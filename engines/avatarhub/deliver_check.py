# -*- coding: utf-8 -*-
"""一键交付自检：把 A→D 阶段的所有验收手段串成一次「交付仪式」，给集成商/创作者
照单核对即可上线。纯编排——不改任何服务、不做破坏性动作。

阶段（按序，可 --only / --skip 裁剪）：
  1. preflight   离线预检   doctor.py --preflight   （GPU/conda/端口/多卡副本/磁盘/流式端点）
  2. hub         在线探活   GET <hub>/health        （默认不自动启服务；--start 才拉起）
  3. doctor      联机体检   doctor.py                （服务/角色/容量/观众/流式就绪）
  4. provenance  验真闭环   GET status/pubkey + 往返 /verify （能力/Ed25519公钥/正负样本·GPU无关·秒级）
  5. brand       白标持久化 GET/POST /api/brand 往返 （写读一致后还原·非破坏·GPU无关·秒级）
  5b.docs        教程/就绪门禁 本地 markdown 渲染 + /help + 就绪端点 + 手机免证书 plist（GPU无关·秒级）
  6. acceptance  回归门禁   acceptance.py            （含流式 TTS 一致性门禁；--full 加重负载）
  7. soak        运行期体检 conv_soak.py            （可选：--soak 才跑。并发对话 live 压测，
                 实测 TTFA/成功率、准入闸不越限、显存不泄漏；吃 GPU，故默认关闭）
  8. brevity     简答收益   audience_loadtest.py sim --compare-service （信息项，无需 GPU）

容灾演练（不在本流程内·手动）：_fault_drill.py —— 中途杀核心 TTS 验证看门狗自愈，
  会真杀 fish_speech_server，破坏性强，勿在直播中跑；仅按需手动执行。

退出码：0=全通过(可交付)  1=有警告(可上线但建议处理)  2=有严重问题/失败(不可交付)

用法：
  python deliver_check.py                         # 标准交付自检（不含 soak）
  python deliver_check.py --full                  # 含重负载回归
  python deliver_check.py --soak                  # 额外跑运行期并发体检(需 Hub 在线+GPU)
  python deliver_check.py --start --hub http://127.0.0.1:9000
  python deliver_check.py --only preflight,brevity   # 无 Hub 也能跑的离线子集
  python deliver_check.py --json
"""
import os
import sys
import json
import time
import argparse
import subprocess
import urllib.request
from pathlib import Path

try:
    # line_buffering=True：长流程(联机体检/回归动辄数分钟)的进度即时可见，不被块缓冲憋住
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
DEFAULT_HUB = os.environ.get("ACCEPT_HUB", "http://127.0.0.1:9000")

# level 序：ok < warn < crit（fail 归 crit 级，统一最严重）
_LEVEL_RANK = {"ok": 0, "warn": 1, "crit": 2, "fail": 2, "skip": 0}
_ICONS = {"ok": "✓", "warn": "⚠", "crit": "✗", "fail": "✗", "skip": "·"}

STAGE_ORDER = ["preflight", "hub", "envsnap", "doctor", "provenance", "brand",
               "docs", "acceptance", "soak", "brevity", "envguard"]


def _icons():
    try:
        "✓⚠✗·".encode(sys.stdout.encoding or "utf-8")
        return _ICONS
    except Exception:
        return {"ok": "[OK]", "warn": "[! ]", "crit": "[X ]", "fail": "[X ]", "skip": "[ -]"}


def _level_from_doctor(code):
    """doctor 退出码 → level：0 健康 / 1 警告 / 2 严重。"""
    return {0: "ok", 1: "warn", 2: "crit"}.get(code, "crit")


def _level_from_acceptance(code):
    """acceptance：0 全通过；非 0 视为失败门禁。"""
    return "ok" if code == 0 else "fail"


def _aggregate(results):
    """把各阶段 level 聚合为总判定 + 退出码。results: [{stage,level,summary}]。
    纯函数，便于单测。返回 (overall_level, exit_code)。"""
    worst = "ok"
    for r in results:
        if _LEVEL_RANK.get(r["level"], 0) > _LEVEL_RANK.get(worst, 0):
            worst = r["level"]
    exit_code = {"ok": 0, "warn": 1, "crit": 2, "fail": 2}.get(worst, 2)
    return worst, exit_code


def _run(cmd, timeout):
    """跑子进程，返回 (exit_code, output)。脚本缺失/异常不抛，归一为非 0。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        p = subprocess.run(cmd, cwd=str(HERE), env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", errors="replace")
        return 124, out + f"\n[超时 {timeout}s]"
    except FileNotFoundError as e:
        return 127, f"[脚本/解释器缺失] {e}"
    except Exception as e:
        return 1, f"[执行异常] {e}"


def probe_hub(hub, timeout=4.0):
    try:
        with urllib.request.urlopen(hub.rstrip("/") + "/health", timeout=timeout):
            return True
    except Exception:
        return False


def _get_json(url, timeout=6.0):
    """GET → (ok, dict|errstr)。任何异常归一为 (False, 原因)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return False, str(e)


def _get_text(url, timeout=6.0):
    """GET → (ok, text|errstr)。任何异常归一为 (False, 原因)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, str(e)


def _post_json(url, payload, timeout=12.0):
    """POST JSON → (ok, dict|errstr)。"""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return False, str(e)


def _plist_ok(text):
    """校验 iOS 描述文件：合法 XML plist + 含 root 证书载荷 + 内嵌 DER 可解析为证书。
    返回 True 或错误原因字符串（便于门禁直接展示人话）。"""
    try:
        from xml.dom import minidom
        minidom.parseString(text)
    except Exception as e:
        return "plist 非合法 XML：%s" % e
    if "com.apple.security.root" not in text:
        return "缺 com.apple.security.root 证书载荷"
    import re as _re
    import base64 as _b64
    m = _re.search(r"<data>([A-Za-z0-9+/=\s]+)</data>", text)
    if not m:
        return "无内嵌证书 <data>"
    try:
        der = _b64.b64decode(m.group(1))
        from cryptography import x509
        x509.load_der_x509_certificate(der)
    except ImportError:
        return True   # 无 cryptography：DER 已能 base64 解出，放行（宿主机通常都有）
    except Exception as e:
        return "内嵌证书无法解析：%s" % e
    return True


def _tiny_wav_b64():
    """就地合成一段 1s/16k/mono/PCM16 的正弦音（GPU 无关），返回 (wav_bytes, base64_str)。
    用作验真往返的载体：足够长以承载 LSB 水印帧。"""
    import io
    import math
    import wave
    import struct
    import base64 as _b64
    sr, secs = 16000, 1.0
    n = int(sr * secs)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            v = int(1000 * math.sin(2 * math.pi * 220 * i / sr))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    raw = buf.getvalue()
    return raw, _b64.b64encode(raw).decode("ascii")


def _tail(out, n=12):
    lines = [l for l in (out or "").splitlines() if l.strip()]
    return "\n".join("      " + l for l in lines[-n:])


# ── 各阶段 ──────────────────────────────────────────────────────────
def stage_preflight(args):
    if not (HERE / "doctor.py").is_file():
        return {"level": "warn", "summary": "doctor.py 缺失，跳过离线预检", "out": ""}
    code, out = _run([PY, "doctor.py", "--preflight"], timeout=120)
    lvl = _level_from_doctor(code)
    return {"level": lvl, "summary": "离线预检 " + ("通过" if lvl == "ok" else
            ("有警告" if lvl == "warn" else "有严重问题")), "out": out}


def stage_hub(args):
    up = probe_hub(args.hub)
    if up:
        return {"level": "ok", "summary": f"Hub 在线 {args.hub}", "out": ""}
    if not args.start:
        return {"level": "crit", "summary": f"Hub 未响应 {args.hub}/health"
                "（先运行 start_all_services.bat，或加 --start 自动拉起）", "out": ""}
    # --start：拉起服务并轮询就绪（不阻塞式杀进程，交给 .bat 自身处理）
    bat = HERE / "start_all_services.bat"
    if not bat.is_file():
        return {"level": "crit", "summary": "start_all_services.bat 缺失，无法 --start", "out": ""}
    try:
        subprocess.Popen(["cmd", "/c", "start", "", str(bat)], cwd=str(HERE), shell=False)
    except Exception as e:
        return {"level": "crit", "summary": f"拉起服务失败: {e}", "out": ""}
    deadline = time.time() + args.start_timeout
    while time.time() < deadline:
        if probe_hub(args.hub):
            return {"level": "ok", "summary": f"已拉起并就绪 {args.hub}"
                    f"（{int(args.start_timeout-(deadline-time.time()))}s）", "out": ""}
        time.sleep(3)
    return {"level": "crit", "summary": f"拉起后 {args.start_timeout}s 内未就绪", "out": ""}


def stage_doctor(args):
    if not (HERE / "doctor.py").is_file():
        return {"level": "warn", "summary": "doctor.py 缺失，跳过联机体检", "out": ""}
    code, out = _run([PY, "doctor.py", "--profile", args.profile], timeout=180)
    lvl = _level_from_doctor(code)
    return {"level": lvl, "summary": "联机体检 " + ("健康" if lvl == "ok" else
            ("有警告" if lvl == "warn" else "有严重问题")), "out": out}


def stage_provenance(args):
    """验真闭环门禁（GPU 无关 · 秒级）：把「AI生成·C2PA可验真」从口号固化成自动回归。
      1) status   能力探测：loaded / public_verifiable / Ed25519
      2) pubkey   公钥可被独立解析为合法 Ed25519（第三方离线验签前提）
      3) 正样本   就地嵌入水印+签名 → 经 Hub /verify → 应 has_watermark & signature_valid
      4) 负样本   未签名音频经 Hub /verify → 应 has_watermark=false（证明验真能区分）
    任一硬性断言失败→crit；缺验签库/能力降级→warn；模块缺失→skip。"""
    base = args.hub.rstrip("/")
    lines, worst = [], "ok"

    def bump(lvl):
        nonlocal worst
        if _LEVEL_RANK.get(lvl, 0) > _LEVEL_RANK.get(worst, 0):
            worst = lvl

    # 1) 能力探测
    ok, st = _get_json(base + "/api/provenance/status")
    if not ok:
        return {"level": "warn", "summary": "验真能力未启用/不可达（provenance 未加载）",
                "out": "      status: " + str(st)}
    loaded = bool(st.get("loaded"))
    pubver = bool(st.get("public_verifiable"))
    alg = st.get("signature_alg", "")
    if not loaded:
        bump("crit"); lines.append("✗ status.loaded=false（验签私钥/库未就绪）")
    else:
        lines.append("✓ status.loaded=true alg=%s c2pa=%s" % (alg, st.get("c2pa_embedded")))
    if not pubver:
        bump("warn"); lines.append("⚠ public_verifiable=false（无法对外离线验签）")
    if alg and alg != "Ed25519":
        bump("warn"); lines.append("⚠ 签名算法非 Ed25519（为 %s）" % alg)

    # 2) 公钥可独立解析为合法 Ed25519
    ok, pk = _get_json(base + "/api/provenance/pubkey")
    pem = (pk or {}).get("public_key", "") if ok else ""
    if not pem or "BEGIN PUBLIC KEY" not in pem:
        bump("warn"); lines.append("⚠ pubkey 未返回有效 PEM")
    else:
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            k = load_pem_public_key(pem.encode("utf-8"))
            if isinstance(k, Ed25519PublicKey):
                lines.append("✓ pubkey 解析为合法 Ed25519（第三方可离线验签）")
            else:
                bump("warn"); lines.append("⚠ pubkey 非 Ed25519 类型")
        except ImportError:
            lines.append("· pubkey 含 PEM 头（未装 cryptography，跳过强校验）")
        except Exception as e:
            bump("warn"); lines.append("⚠ pubkey 解析失败：%s" % e)

    # 3)+4) 验真往返：就地嵌入 → 经 Hub /verify（正样本），未签名音频（负样本）
    try:
        sys.path.insert(0, str(HERE))
        import provenance as _prov
        plain_bytes, plain_b64 = _tiny_wav_b64()
        att = _prov.attach_credentials(plain_bytes, model="deliver_check", profile="自检",
                                       ai_generated=True)
        import base64 as _b64
        signed_b64 = _b64.b64encode(att["audio_bytes"]).decode("ascii")

        ok, pos = _post_json(base + "/api/provenance/verify", {"audio_base64": signed_b64})
        if not ok:
            bump("crit"); lines.append("✗ 正样本 /verify 调用失败：%s" % pos)
        elif pos.get("has_watermark") and pos.get("signature_valid"):
            lines.append("✓ 正样本经 Hub 验真：has_watermark & signature_valid")
        else:
            bump("crit"); lines.append("✗ 正样本验真未通过：%s" %
                                       {k: pos.get(k) for k in ("has_watermark", "signature_valid")})

        ok, neg = _post_json(base + "/api/provenance/verify", {"audio_base64": plain_b64})
        if not ok:
            bump("warn"); lines.append("⚠ 负样本 /verify 调用失败：%s" % neg)
        elif neg.get("has_watermark"):
            bump("crit"); lines.append("✗ 负样本被误判为有水印（验真无法区分未签名内容）")
        else:
            lines.append("✓ 负样本经 Hub 验真：has_watermark=false（能区分未签名）")

        # 图片/视频产出端 C2PA 往返：就地嵌入 → 经 Hub /verify_media 读回（GPU 无关）
        try:
            import io as _io
            from PIL import Image as _Img
            _buf = _io.BytesIO()
            _Img.new("RGB", (64, 64), (80, 120, 200)).save(_buf, format="PNG")
            emb = _prov.embed_c2pa(_buf.getvalue(), "image/png",
                                   model="deliver_check", profile="自检", ai_generated=True)
            if not emb:
                lines.append("· c2pa-python/证书未就绪，跳过图片 C2PA 往返")
            else:
                img_b64 = _b64.b64encode(emb).decode("ascii")
                ok, mv = _post_json(base + "/api/provenance/verify_media",
                                    {"media_base64": img_b64, "mime_type": "image/png"})
                if ok and mv.get("has_c2pa") and mv.get("integrity_ok"):
                    lines.append("✓ 图片 C2PA 往返：嵌入→Hub verify_media 读回 has_c2pa & integrity_ok")
                else:
                    bump("warn"); lines.append("⚠ 图片 C2PA 往返未通过：%s" %
                                               {k: (mv or {}).get(k) for k in ("has_c2pa", "integrity_ok")})
        except ImportError:
            lines.append("· 未装 Pillow，跳过图片 C2PA 往返（不影响音频验真结论）")
        except Exception as e:
            bump("warn"); lines.append("⚠ 图片 C2PA 往返异常：%s" % e)
    except ImportError:
        bump("warn"); lines.append("⚠ 无法 import provenance（跳过验真往返）")
    except Exception as e:
        bump("warn"); lines.append("⚠ 验真往返异常：%s" % e)

    summ = {"ok": "验真闭环通过（能力/公钥/正负样本全绿）",
            "warn": "验真闭环有警告（能力降级，建议处理）",
            "crit": "验真闭环失败（签名链/区分能力异常·不可交付）"}.get(worst, "已校验")
    return {"level": worst, "summary": summ, "out": "\n".join("      " + l for l in lines)}


def stage_brand(args):
    """白标持久化门禁（GPU 无关 · 秒级 · 非破坏）：验证「一次配置·整机生效」可落盘。
      1) GET /api/brand 可达       2) 写探针→读回一致（跨终端可持久化）
      3) 还原原始配置（自检零残留：原为空则清空回出厂）
    持久化属增值能力 → 失败不阻断交付（最高 warn）：前端取不到会回退 localStorage 仍可用。"""
    base = args.hub.rstrip("/")
    ok, cur = _get_json(base + "/api/brand")
    if not ok or not isinstance(cur, dict) or "config" not in cur:
        return {"level": "warn", "summary": "白标持久化端点不可用（前端将回退 localStorage）",
                "out": "      GET /api/brand: " + str(cur)}
    original = cur.get("config") or {}
    lines, worst = ["✓ GET /api/brand 可达"], "ok"

    def bump(lvl):
        nonlocal worst
        if _LEVEL_RANK.get(lvl, 0) > _LEVEL_RANK.get(worst, 0):
            worst = lvl

    probe = {"name": "__selfcheck_probe__", "color": "1,2,3"}
    try:
        ok, _r = _post_json(base + "/api/brand", {"config": probe})
        if not ok:
            bump("warn"); lines.append("⚠ POST 写入失败（持久化不可用）：%s" % _r)
        else:
            ok2, after = _get_json(base + "/api/brand")
            got = (after or {}).get("config", {}) if ok2 else {}
            if got.get("name") == probe["name"] and got.get("color") == probe["color"]:
                lines.append("✓ 写后读回一致（跨终端可持久化）")
            else:
                bump("warn"); lines.append("⚠ 写后读回不一致：%s" % got)
    finally:
        # 还原原始配置：原为空 → POST {} 删文件回出厂；自检绝不污染线上品牌
        _post_json(base + "/api/brand", {"config": original})
        ok3, restored = _get_json(base + "/api/brand")
        rc = (restored or {}).get("config", {}) if ok3 else None
        if rc == original:
            lines.append("✓ 已还原原始品牌配置（自检无残留）")
        else:
            bump("warn"); lines.append("⚠ 还原后与原配置不符，请核对 data/brand.json：%s" % rc)

    summ = {"ok": "白标持久化就绪（写读一致·已还原）",
            "warn": "白标持久化有警告（可回退 localStorage·不阻断交付）"}.get(worst, "已校验")
    return {"level": worst, "summary": summ, "out": "\n".join("      " + l for l in lines)}


def stage_docs(args):
    """教程/就绪/免证书 可维护性门禁（GPU 无关·秒级·非破坏）：
      1) 三篇图文教程本地存在 + markdown 可渲染出结构（改坏文档/删文件即报红·不依赖 Hub）
      2) Hub 在线时：/help 路由可达；就绪三灯依赖的 /api/setup/status(含 services+profile) 可达
      3) 手机免证书：monitor /cert.mobileconfig 是合法 iOS plist + 内嵌证书可解析
    交付物损坏(教程/就绪端点/证书)=crit；可选服务(手机中继)离线或未装 markdown=warn。"""
    base = args.hub.rstrip("/")
    mon = args.monitor.rstrip("/")
    lines, worst = [], "ok"

    def bump(lvl):
        nonlocal worst
        if _LEVEL_RANK.get(lvl, 0) > _LEVEL_RANK.get(worst, 0):
            worst = lvl

    # 1) 教程源文件本地校验（确定性·不依赖 Hub）——与 avatar_hub _HELP_DOCS 同名单
    docs_files = [("安装教程", "安装教程_图文版.md"),
                  ("使用教程", "使用教程_图文版.md"),
                  ("手机同传扫码授权", "手机同传扫码授权全流程.md")]
    try:
        import markdown as _md
        have_md = True
    except Exception:
        have_md = False
        bump("warn"); lines.append("⚠ 宿主未装 markdown，教程页将降级为纯文本")
    for label, fn in docs_files:
        p = HERE / fn
        if not p.is_file():
            bump("crit"); lines.append("✗ 教程源缺失：%s" % fn); continue
        if not have_md:
            lines.append("· 教程[%s] 存在（未装 markdown，跳过渲染校验）" % label); continue
        try:
            html = _md.markdown(p.read_text(encoding="utf-8"),
                                extensions=["tables", "fenced_code", "sane_lists"])
            if len(html) < 200 or ("<h2" not in html and "<h1" not in html):
                bump("crit"); lines.append("✗ 教程[%s] 渲染异常（无标题/内容过短）" % label)
            else:
                lines.append("✓ 教程[%s] 渲染正常" % label)
        except Exception as e:
            bump("crit"); lines.append("✗ 教程[%s] markdown 渲染报错：%s" % (label, e))

    # 2) Hub 在线时校验 /help 路由 + 就绪三灯数据源
    if probe_hub(base, 2.0):
        ok, _h = _get_text(base + "/help?doc=interp")
        if ok and ("<h2" in _h or "<table" in _h) and "未找到教程文件" not in _h:
            lines.append("✓ /help 教程中心在线渲染正常")
        else:
            bump("crit"); lines.append("✗ /help 在线渲染异常（路由/模板问题）")
        ok, st = _get_json(base + "/api/setup/status")
        ids = {s.get("id") for s in (st.get("steps") or [])} if (ok and isinstance(st, dict)) else set()
        if {"services", "profile"} <= ids:
            lines.append("✓ /api/setup/status 就绪（首启三灯数据源含 services+profile）")
        else:
            bump("crit"); lines.append("✗ /api/setup/status 异常（首启就绪灯将失效）：%s" %
                                       (sorted(ids) if ids else st))
        ok, ms = _get_json(base + "/api/monitor/status")
        if ok and isinstance(ms, dict) and "reachable" in ms:
            lines.append("✓ /api/monitor/status 就绪（手机灯数据源）")
        else:
            bump("warn"); lines.append("⚠ /api/monitor/status 异常（手机就绪灯将显未知）")
    else:
        bump("warn"); lines.append("⚠ Hub 未在线，跳过 /help 与就绪端点在线校验（本地教程校验已完成）")

    # 3) 手机免证书（可选服务 monitor_relay:7878）
    ok, mc = _get_text(mon + "/cert.mobileconfig", timeout=3.0)
    if not ok:
        bump("warn"); lines.append("⚠ 手机中继离线，跳过免证书校验（要用手机同传再起 start_monitor_relay.bat）")
    else:
        v = _plist_ok(mc)
        if v is True:
            lines.append("✓ /cert.mobileconfig 合法（iOS 免证书描述文件可装）")
        else:
            bump("crit"); lines.append("✗ /cert.mobileconfig 异常：%s" % v)
        ok2, pem = _get_text(mon + "/cert.pem", timeout=3.0)
        if ok2 and "BEGIN CERTIFICATE" in pem:
            lines.append("✓ /cert.pem 可下载（安卓/桌面装信任）")
        else:
            bump("warn"); lines.append("⚠ /cert.pem 异常（不影响 iOS 描述文件）")
        # 证书链一致性（中继单点算清，此处只消费）：出示的叶子须由 /cert.pem 那张 CA 签发，
        # 否则“装了 CA 仍告警”。这正是本项目踩过的坑，固化成硬门禁。
        ok3, mi = _get_json(mon + "/info", timeout=3.0)
        cc = mi.get("cert_chain_ok") if (ok3 and isinstance(mi, dict)) else None
        if cc is False:
            bump("crit"); lines.append("✗ 证书链不一致：7879 出示的叶子非当前 CA 签发"
                                       "（装了 CA 仍会告警）→ 重启 monitor_relay 重签叶子")
        elif cc is True:
            lines.append("✓ 证书链一致（叶子由当前 CA 签发 · CA 指纹 %s）" % (mi.get("ca_fp") or "?"))
        else:
            lines.append("· 证书链状态未知（纯 http 或旧版中继，跳过）")

    summ = {"ok": "教程/就绪/免证书 全绿",
            "warn": "教程/就绪/免证书 有警告（多为可选服务离线/未装 markdown）",
            "crit": "教程/就绪/免证书 有失败（交付物损坏·需修复）"}.get(worst, "已校验")
    return {"level": worst, "summary": summ, "out": "\n".join("      " + l for l in lines)}


def stage_acceptance(args):
    if not (HERE / "acceptance.py").is_file():
        return {"level": "warn", "summary": "acceptance.py 缺失，跳过回归门禁", "out": ""}
    cmd = [PY, "acceptance.py"] + (["--full"] if args.full else [])
    code, out = _run(cmd, timeout=(1800 if args.full else 720))
    lvl = _level_from_acceptance(code)
    # 抽取总表那一行做 summary
    line = next((l for l in reversed(out.splitlines()) if "总计" in l), "")
    return {"level": lvl, "summary": ("回归门禁 " + ("全通过" if lvl == "ok" else "存在失败项"))
            + (f" · {line.strip()}" if line else ""), "out": out}


def stage_soak(args):
    """运行期并发体检（可选·live·吃 GPU）：并发多会话真实对话压测，实测 TTFA/成功率，
    并核对「准入闸 active 不越 K」「显存首尾无单调下滑(无泄漏)」。conv_soak 退出码 0=PASS / 1=CHECK；
    CHECK 归 warn（运行期偶发不阻断交付结论，但提示复查），脚本崩/缺失归 warn/skip。"""
    if not (HERE / "conv_soak.py").is_file():
        return {"level": "skip", "summary": "conv_soak.py 缺失，跳过运行期体检", "out": ""}
    code, out = _run([PY, "conv_soak.py", str(args.soak_secs), str(args.soak_workers)],
                     timeout=args.soak_secs + 120)
    line = next((l for l in reversed(out.splitlines()) if "SOAK" in l), "")
    lvl = "ok" if code == 0 else "warn"
    return {"level": lvl, "summary": "运行期并发体检 "
            + (line.strip() or ("PASS" if code == 0 else "CHECK")), "out": out}


def stage_brevity(args):
    if not (HERE / "audience_loadtest.py").is_file():
        return {"level": "skip", "summary": "audience_loadtest.py 缺失，跳过", "out": ""}
    code, out = _run([PY, "audience_loadtest.py", "sim", "--compare-service", "8,4",
                      "--qps", "0.1", "--duration", "600"], timeout=120)
    line = next((l for l in reversed(out.splitlines()) if "结论" in l), "")
    # 信息项：不阻断交付（除非脚本崩）
    lvl = "ok" if code == 0 else "warn"
    return {"level": lvl, "summary": "简答收益(信息项) " + (line.strip() or "已评估"), "out": out}


# ── P14-1 环境指纹契约：进场快照 → 离场对账 ─────────────────────────
# 实锤（2026-07-07）：E2E/验收在角色库留痕（临时角色/激活切换/品牌改写）→ uivr 假红、
# 声测矩阵撞 `_e2e*` 残留。快照「角色列表+激活角色+品牌配置」，全部阶段跑完后对账：
# 有漂移=测试没收拾干净（warn 不阻断——漂移是测试卫生问题，不代表产品坏了）。
_ENV_SNAP: dict = {}


def _env_fingerprint(hub: str) -> "dict|None":
    ok1, prof = _get_json(hub.rstrip("/") + "/profiles?fields=name")
    ok2, brand = _get_json(hub.rstrip("/") + "/api/brand")
    if not ok1:
        return None
    plist = prof.get("profiles", []) if isinstance(prof, dict) else prof
    return {"roles": sorted(p.get("name", "") for p in plist),
            "active": (prof.get("active", "") if isinstance(prof, dict) else ""),
            "brand": (brand.get("config", {}) if ok2 and isinstance(brand, dict) else {})}


def stage_envsnap(args):
    snap = _env_fingerprint(args.hub)
    if snap is None:
        return {"level": "warn", "summary": "环境快照失败（/profiles 不可达），离场对账将跳过", "out": ""}
    _ENV_SNAP.update(snap)
    return {"level": "ok", "summary": f"环境快照：{len(snap['roles'])} 角色 · 激活「{snap['active'] or '-'}」",
            "out": ""}


def stage_envguard(args):
    if not _ENV_SNAP:
        return {"level": "skip", "summary": "无进场快照，跳过对账", "out": ""}
    cur = _env_fingerprint(args.hub)
    if cur is None:
        return {"level": "warn", "summary": "离场快照失败（/profiles 不可达）", "out": ""}
    drifts = []
    added = sorted(set(cur["roles"]) - set(_ENV_SNAP["roles"]))
    removed = sorted(set(_ENV_SNAP["roles"]) - set(cur["roles"]))
    if added:
        drifts.append("新增角色残留: " + "、".join(added))
    if removed:
        drifts.append("角色被删: " + "、".join(removed))
    if cur["active"] != _ENV_SNAP["active"]:
        drifts.append(f"激活角色被顶: 「{_ENV_SNAP['active']}」→「{cur['active']}」")
    if cur["brand"] != _ENV_SNAP["brand"]:
        drifts.append("品牌配置被改写未还原")
    if not drifts:
        return {"level": "ok", "summary": "环境对账全平（角色/激活/品牌零漂移）", "out": ""}
    return {"level": "warn", "summary": "环境漂移：" + "；".join(drifts)
            + " —— 测试未清场，uivr 基线/后续验收可能被污染", "out": "\n".join(drifts)}


_STAGES = {
    "preflight": ("离线预检", stage_preflight),
    "hub": ("Hub 探活", stage_hub),
    "envsnap": ("环境快照", stage_envsnap),
    "doctor": ("联机体检", stage_doctor),
    "provenance": ("验真闭环", stage_provenance),
    "brand": ("白标持久化", stage_brand),
    "docs": ("教程/就绪门禁", stage_docs),
    "acceptance": ("回归门禁", stage_acceptance),
    "soak": ("运行期体检", stage_soak),
    "brevity": ("简答收益", stage_brevity),
    "envguard": ("环境对账", stage_envguard),
}


def _select_stages(args):
    stages = list(STAGE_ORDER)
    want = set(s.strip() for s in args.only.split(",") if s.strip()) if args.only else None
    # soak 默认不跑：仅 --soak 或 --only 显式点名时纳入（避免标准自检意外吃 GPU）
    if not args.soak and not (want and "soak" in want):
        stages = [s for s in stages if s != "soak"]
    if want is not None:
        stages = [s for s in stages if s in want]
    if args.skip:
        drop = set(s.strip() for s in args.skip.split(",") if s.strip())
        stages = [s for s in stages if s not in drop]
    return stages


def main():
    ap = argparse.ArgumentParser(description="一键交付自检（编排 doctor/acceptance/loadtest）")
    ap.add_argument("--hub", default=DEFAULT_HUB)
    ap.add_argument("--monitor", default=os.environ.get("MONITOR_URL", "http://127.0.0.1:7878"),
                    help="手机中继(monitor_relay)地址，供教程门禁校验免证书描述文件")
    ap.add_argument("--profile", default="",
                    help="主角色名；省略则由 doctor 自动跟随当前激活角色")
    ap.add_argument("--full", action="store_true", help="acceptance 含重负载回归")
    ap.add_argument("--soak", action="store_true", help="额外跑运行期并发体检 conv_soak（需 Hub 在线+GPU）")
    ap.add_argument("--soak-secs", dest="soak_secs", type=int, default=45, help="soak 时长秒（默认 45）")
    ap.add_argument("--soak-workers", dest="soak_workers", type=int, default=3, help="soak 并发会话数（默认 3）")
    ap.add_argument("--start", action="store_true", help="Hub 未起时自动 start_all_services.bat 并等待就绪")
    ap.add_argument("--start-timeout", dest="start_timeout", type=float, default=180)
    ap.add_argument("--only", default="", help="只跑逗号分隔阶段，如 preflight,brevity")
    ap.add_argument("--skip", default="", help="跳过逗号分隔阶段")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    icon = _icons()
    stages = _select_stages(args)
    results = []
    if not args.json:
        print("=" * 64)
        print("  AvatarHub 一键交付自检  deliver_check   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("  目标 Hub: %s   阶段: %s%s" % (args.hub, "、".join(stages),
                                              " (含重负载)" if args.full else ""))
        print("=" * 64)

    force_skip = set()    # hub 挂掉后，联机阶段被标记跳过（不真正执行）
    for key in stages:
        title, fn = _STAGES[key]
        if key in force_skip:
            r = {"level": "skip", "summary": "Hub 未就绪，跳过", "sec": 0.0, "out": ""}
        else:
            if not args.json:
                print(f"\n▶ [{key}] {title} …", flush=True)
            t0 = time.time()
            r = fn(args)
            r["sec"] = round(time.time() - t0, 1)
        r.update(stage=key, title=title)
        results.append(r)
        if not args.json:
            print(f"  {icon[r['level']]} {r['summary']}  ({r.get('sec', 0.0)}s)")
            if r["level"] in ("crit", "fail") and r.get("out"):
                print("    ── 末尾输出 ──")
                print(_tail(r["out"]))
        # hub 严重失败 → 后续联机阶段（doctor/acceptance）无意义，标记跳过（离线 brevity 仍跑）
        if key == "hub" and r["level"] == "crit":
            force_skip.update({"envsnap", "doctor", "provenance", "brand",
                               "acceptance", "soak", "envguard"})

    overall, code = _aggregate(results)

    # 落盘报告（供留痕/CI）
    only = bool(args.only.strip())
    mode = ("only:" + args.only if only else ("full" if args.full else "standard"))
    report = {"ts": time.time(), "hub": args.hub, "overall": overall, "exit_code": code,
              "mode": mode, "partial": only,
              "full": args.full,
              "stages": [{k: r[k] for k in ("stage", "title", "level", "summary", "sec")}
                         for r in results]}
    report_name = "deliver_report_partial.json" if only else "deliver_report.json"
    try:
        (HERE / "logs").mkdir(exist_ok=True)
        report_path = HERE / "logs" / report_name
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return code

    print("\n" + "=" * 64)
    verdict = {"ok": "✅ 全部通过 · 可交付上线", "warn": "⚠ 有警告 · 可上线但建议先处理",
               "crit": "✗ 存在严重问题 · 暂不可交付", "fail": "✗ 回归失败 · 暂不可交付"}.get(overall)
    print("  交付结论：" + verdict)
    print("  报告：logs/%s%s" % (report_name, "（--only 部分跑,不覆盖主报告）" if only else ""))
    print("=" * 64)
    return code


if __name__ == "__main__":
    sys.exit(main())
