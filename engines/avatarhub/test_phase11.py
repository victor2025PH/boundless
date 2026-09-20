# -*- coding: utf-8 -*-
"""Phase 11 测试：C2PA 风格凭证 + 鲁棒水印 + 软绑定 (T46-T50)

核心逻辑离线可测（provenance 模块）；在线部分仅在 Hub 运行时执行。
"""
import sys, os, io, wave, math, array, base64, json
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS = 0; FAIL = 0; SKIP = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  [PASS] {name} {detail}")
    else:    FAIL += 1; print(f"  [FAIL] {name} {detail}")
def skip(name, why=""):
    global SKIP; SKIP += 1; print(f"  [SKIP] {name} {why}")

print("=" * 55)
print(" Phase 11 测试：合规溯源（C2PA + 鲁棒水印）")
print("=" * 55)

def make_wav(seconds=2.0, rate=16000, freq=220.0) -> bytes:
    n = int(seconds * rate)
    samples = array.array("h", (int(12000 * math.sin(2*math.pi*freq*i/rate)) for i in range(n)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()

import provenance as P
wav = make_wav()

# ── T46: 凭证生成 + 验证 ──────────────────────────
print("\n--- T46: 生成可验证 C2PA 风格凭证 ---")
try:
    res = P.attach_credentials(wav, model="xtts", profile="测试角色")
    check("attach 成功且已打水印", res["ok"] and res["watermarked"], f"pid={res['payload_id'][:8]}")
    v = P.verify_credentials(res["audio_bytes"])
    check("verify 检出水印", v["has_watermark"], f"pid={v['payload_id'][:8]}")
    check("manifest 签名有效", v["signature_valid"])
    check("机器可读 AI 生成标记", v["ai_generated"] is True)
    # IPTC digitalSourceType 存在
    acts = v["manifest"]["assertions"][0]["data"]["actions"][0]
    check("含 IPTC trainedAlgorithmicMedia", "trainedAlgorithmicMedia" in acts.get("digitalSourceType",""))
except Exception as e:
    check("attach/verify", False, str(e))

# ── T47: 水印鲁棒性（追加/裁剪/重封装）────────────
print("\n--- T47: 水印抗追加/裁剪/重封装 ---")
try:
    res = P.attach_credentials(wav, model="cosyvoice", profile="鲁棒测试")
    wm = res["audio_bytes"]; pid = res["payload_id"]
    # 重新封装为 WAV（解析→重写，模拟格式保留编辑）
    parsed = P._read_wav_pcm16(wm)
    remux = P._write_wav_pcm16(parsed[0], parsed[1])
    check("重封装后仍可检出", P.extract_audio_watermark(remux) == pid)
    # 裁剪后半段（丢弃前 30%）
    params, samples = parsed
    cut = array.array("h", samples[int(len(samples)*0.3):])
    cropped = P._write_wav_pcm16(params, cut)
    check("裁剪 30% 后仍可检出", P.extract_audio_watermark(cropped) == pid)
except Exception as e:
    check("水印鲁棒性", False, str(e))

# ── T48: 软绑定解析 ───────────────────────────────
print("\n--- T48: 软绑定 manifest 解析 ---")
try:
    res = P.attach_credentials(wav, model="xtts", profile="软绑定")
    rec = P.resolve_manifest(res["payload_id"])
    check("按 payload_id 解析到 manifest", rec is not None and "manifest" in rec)
    check("解析的 manifest 可验签", P.verify_signature(rec["manifest"], rec["signature"]))
    check("未知 id 解析为空", P.resolve_manifest("deadbeef"*4) is None)
except Exception as e:
    check("软绑定解析", False, str(e))

# ── T49: 篡改检测（manifest 被改后验签失败）────────
print("\n--- T49: 篡改导致验签失败 ---")
try:
    res = P.attach_credentials(wav, model="xtts", profile="篡改测试")
    rec = P.resolve_manifest(res["payload_id"])
    tampered = dict(rec["manifest"]); tampered["claim_generator"] = "FakeTool/9.9"
    check("篡改后验签失败", P.verify_signature(tampered, rec["signature"]) is False)
    check("原始验签通过", P.verify_signature(rec["manifest"], rec["signature"]) is True)
except Exception as e:
    check("篡改检测", False, str(e))

# ── T49b: Ed25519 非对称签名 公开可验性 ─────────────
print("\n--- T49b: Ed25519 公开可验（仅凭公钥验真）---")
try:
    if not getattr(P, "_HAS_ED25519", False):
        skip("Ed25519", "(未装 cryptography)")
    else:
        res = P.attach_credentials(wav, model="xtts", profile="非对称签名")
        rec = P.resolve_manifest(res["payload_id"])
        check("新凭证用 Ed25519 签名", rec["alg"] == "Ed25519", f"alg={rec['alg']}")
        pem = P.public_key_pem()
        check("可导出公钥 PEM", pem.startswith("-----BEGIN PUBLIC KEY-----"))
        # 第三方仅凭公钥验签（不触本机私钥）
        ok = P.verify_with_public_key(rec["manifest"], rec["signature"], pem)
        check("仅凭公钥即可验真", ok is True)
        # 篡改后公钥验签必败
        bad = dict(rec["manifest"]); bad["claim_generator"] = "Fake/9"
        check("篡改后公钥验签失败", P.verify_with_public_key(bad, rec["signature"], pem) is False)
        # 错误公钥验签必败（另生成一把）
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization as _s
        other = Ed25519PrivateKey.generate().public_key().public_bytes(
            _s.Encoding.PEM, _s.PublicFormat.SubjectPublicKeyInfo).decode()
        check("他人公钥验签失败", P.verify_with_public_key(rec["manifest"], rec["signature"], other) is False)
except Exception as e:
    check("Ed25519 公开可验", False, str(e))

# ── T50: AI 生成检测 ──────────────────────────────
print("\n--- T50: AI 生成检测 ---")
try:
    res = P.attach_credentials(wav, model="xtts", profile="检测")
    d1 = P.detect_ai_generated(res["audio_bytes"])
    check("带凭证→判定 AI 生成", d1["ai_generated"] is True and d1["confidence"] >= 0.9, f"{d1}")
    d2 = P.detect_ai_generated(make_wav(freq=440))
    check("无凭证→无溯源信息", d2["ai_generated"] is None, f"{d2}")
except Exception as e:
    check("AI 生成检测", False, str(e))

# ── 在线：Hub 输出强制打标 + 验证端点 ──────────────
print("\n--- 在线: /api/provenance/verify ---")
HUB = os.environ.get("HUB_URL", "http://127.0.0.1:9000")
_online = False
try:
    import requests
    _online = requests.get(f"{HUB}/health", timeout=8).status_code == 200
except Exception:
    _online = False
if _online:
    try:
        import requests
        res = P.attach_credentials(wav, model="probe", profile="端点")
        b64 = base64.b64encode(res["audio_bytes"]).decode()
        r = requests.post(f"{HUB}/api/provenance/verify",
                          json={"audio_base64": b64}, timeout=15)
        check("/api/provenance/verify 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            check("端点检出水印", r.json().get("has_watermark") is True)
    except Exception as e:
        check("/api/provenance/verify", False, str(e))
else:
    skip("provenance 端点", "(Hub 未运行)")

# ── T51: 标准 C2PA 嵌入（CAI 工具可直读）──────────────
print("\n--- T51: 标准 C2PA 嵌入（c2pa-python）---")
if not getattr(P, "_HAS_C2PA", False):
    skip("C2PA 嵌入", "(未装 c2pa-python)")
else:
    try:
        res = P.attach_credentials(wav, model="cosyvoice", profile="C2PA嵌入测试")
        check("attach 含 c2pa_embedded=True", res.get("c2pa_embedded") is True, f"c2pa={res.get('c2pa_embedded')}")
        # 用 read_c2pa 读回验证
        store = P.read_c2pa(res["audio_bytes"], "audio/x-wav")
        check("read_c2pa 读回成功", store is not None and "manifests" in store,
              f"keys={list((store or {}).keys())}")
        if store:
            am = store["manifests"].get(store["active_manifest"], {})
            check("validation_state Valid", store.get("validation_state") == "Valid",
                  f"state={store.get('validation_state')}")
            title = am.get("title", "")
            # 品牌升级(无界 BOUNDLESS)后 C2PA title 随品牌走；两代品牌都算通过
            check("title 含品牌名", any(k in title for k in ("AvatarHub", "BOUNDLESS", "无界")),
                  f"title={title}")
            # IPTC digitalSourceType 存在
            acts = am.get("claim_review", {})
            assertions = am.get("assertions", [])
            found_iptc = any(
                "trainedAlgorithmicMedia" in json.dumps(a)
                for a in assertions)
            check("含 IPTC trainedAlgorithmicMedia", found_iptc)
        # 未装时 embed_c2pa 返回 None（软降级测试）
        orig = P._HAS_C2PA; P._HAS_C2PA = False
        r2 = P.attach_credentials(wav, model="x", profile="降级")
        P._HAS_C2PA = orig
        check("无 c2pa 时软降级(c2pa_embedded=False)", r2.get("c2pa_embedded") is False)
    except Exception as e:
        check("C2PA 嵌入", False, str(e))

print("\n" + "=" * 55)
print(f" 结果: PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}")
print("=" * 55)
sys.exit(1 if FAIL else 0)
