# -*- coding: utf-8 -*-
"""e2e 门禁：声音资产统一管理（/api/voice_assets 系列 + 「声音管理」面板）

依赖本机 hub（默认 http://127.0.0.1:9000，可用环境变量 AVATARHUB_URL 覆盖）与 playwright；
不可用则整体 SKIP（exit 0）。建的临时角色/克隆文件跑完即清，不动真实数据。

覆盖：
  1. 引用回算：一段克隆音绑两个角色 → refs 双命中；被引用时删除被拒（400）
  2. 试听端点：audio/wav 字节直出
  3. 重命名：内容哈希回算 → 改名后引用不丢
  4. 孤儿生命周期：删光引用角色 → orphan=true → bind 找回到新角色 → 再删角色 → 软删入 _trash
  5. 安全：路径穿越拒绝、声音库只读（不可删/不可改名）
  6. 批量清孤儿（AS6）：only 定向清理，被引用文件绝不动；入回收站（kind=clone）可见
  7. 配置包 peek（AS6）：明文/口令加密识别、错口令 400、带口令导入 roundtrip
  8. UI 冒烟：面板打开、克隆条目/孤儿徽章/绑定入口渲染、导入预览卡、无 JS 错误
"""
import base64, io, json, math, os, struct, sys, time, urllib.request, urllib.parse, wave

HUB = os.environ.get("AVATARHUB_URL", "http://127.0.0.1:9000").rstrip("/")
P1, P2, P3 = "_e2e资产A", "_e2e资产B", "_e2e资产C"
CLONE_NAME = "_e2eva"          # clone_voice 落盘名前缀（`{safe_name}_{ts}.wav`）
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok " if cond else "  FAIL ") + name + (("  " + extra) if extra else ""))


def api(method, path, body=None, raw=False):
    req = urllib.request.Request(HUB + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=30) as r:
            content = r.read()
            return r.status, (content if raw else json.loads(content.decode()))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def api_upload(path, fields, filename, file_bytes):
    """multipart/form-data 上传（package_peek / import_package 用）。"""
    boundary = "----e2epkgboundary7f3a"
    parts = []
    for k, v in (fields or {}).items():
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                      f"name=\"{k}\"\r\n\r\n{v}\r\n").encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"file\"; filename=\"{filename}\"\r\n"
                  f"Content-Type: application/octet-stream\r\n\r\n").encode()
                 + file_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(HUB + path, data=b"".join(parts), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def hub_alive():
    try:
        with urllib.request.urlopen(HUB + "/health", timeout=3):
            return True
    except Exception:
        return False


def make_wav_b64(sec=8.0, sr=16000):
    n = int(sec * sr)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            t = i / sr
            if t < sec * 0.85:
                env = 0.55 + 0.35 * math.sin(2 * math.pi * 1.3 * t)
                v = env * 0.5 * (math.sin(2*math.pi*220*t) + 0.4*math.sin(2*math.pi*440*t))
            else:
                v = 0.001
            frames += struct.pack("<h", int(max(-1, min(1, v)) * 30000))
        wf.writeframes(bytes(frames))
    return base64.b64encode(buf.getvalue()).decode()


def enc(s):
    return urllib.parse.quote(s)


def find_asset(assets, name_prefix=CLONE_NAME):
    return [a for a in assets if a["kind"] == "clone" and a["name"].startswith(name_prefix)]


def cleanup():
    for p in (P1, P2, P3):
        api("DELETE", f"/profiles/{enc(p)}")
    # 残留的 _e2eva 克隆文件（含历史失败运行）：先解引用后走接口删（孤儿才可删）
    _, d = api("GET", "/api/voice_assets")
    for a in find_asset(d.get("assets", [])):
        api("DELETE", f"/api/voice_assets/{enc(a['id'])}")
    # 回收目录里的测试残留直接物理清掉
    try:
        clone_dir = d.get("clone_dir") or ""
        trash = os.path.join(clone_dir, "_trash")
        if os.path.isdir(trash):
            for f in os.listdir(trash):
                if CLONE_NAME in f:
                    os.remove(os.path.join(trash, f))
    except OSError:
        pass


def main():
    if not hub_alive():
        print(f"[SKIP] hub 未运行（{HUB}），跳过声音资产 e2e")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] 未安装 playwright，跳过声音资产 e2e")
        return 0

    cleanup()

    # ── 1. 底座：接口活着、结构正确 ──
    st, d = api("GET", "/api/voice_assets")
    check("接口:GET /api/voice_assets", st == 200 and d.get("ok") and isinstance(d.get("assets"), list))

    # ── 2. 克隆一段声音 → 绑两个角色 → refs 双命中 ──
    wav = make_wav_b64()
    st, cd = api("POST", "/api/voice_clone",
                 {"wav_base64": wav, "name": CLONE_NAME, "agreed_terms": True, "user_id": "e2e"})
    check("准备:克隆成功（落盘+水印）", st == 200 and bool(cd.get("voice_b64")))
    vb64 = cd.get("voice_b64") or ""
    for p in (P1, P2):
        api("POST", "/profiles", {"name": p, "voice_b64": vb64, "description": "e2e 资产测试"})
    st, d = api("GET", "/api/voice_assets")
    hits = find_asset(d.get("assets", []))
    check("引用:克隆文件出现在资产列表", len(hits) == 1, extra=f"hits={len(hits)}")
    a = hits[0] if hits else {}
    check("引用:refs 双命中", sorted(a.get("refs", [])) == sorted([P1, P2]), extra=str(a.get("refs")))
    check("引用:非孤儿", a.get("orphan") is False)
    check("元数据:时长/大小可读", a.get("duration_s", 0) > 5 and a.get("size", 0) > 100000,
          extra=f"{a.get('duration_s')}s/{a.get('size')}B")

    # ── 3. 被引用时删除被拒 ──
    st, r = api("DELETE", f"/api/voice_assets/{enc(a['id'])}")
    check("保护:被引用删除→400", st == 400 and P1 in str(r.get("detail", "")), extra=str(r.get("detail"))[:60])

    # ── 4. 试听端点 ──
    st, audio = api("GET", f"/api/voice_assets/{enc(a['id'])}/audio", raw=True)
    check("试听:audio 字节直出", st == 200 and audio[:4] == b"RIFF", extra=f"len={len(audio)}")

    # ── 5. 重命名：内容回算 → 引用不丢 ──
    st, r = api("POST", "/api/voice_assets/rename", {"id": a["id"], "new_name": CLONE_NAME + "_renamed"})
    check("重命名:成功", st == 200 and r.get("ok"), extra=str(r))
    st, d = api("GET", "/api/voice_assets")
    hits = find_asset(d.get("assets", []), CLONE_NAME + "_renamed")
    check("重命名:引用仍双命中", len(hits) == 1 and sorted(hits[0].get("refs", [])) == sorted([P1, P2]),
          extra=str(hits[0].get("refs") if hits else None))
    a = hits[0] if hits else a

    # ── 6. 安全：路径穿越 + 声音库只读 ──
    st, _ = api("GET", "/api/voice_assets/" + enc("clone:..\\..\\secrets.wav") + "/audio")
    check("安全:路径穿越被拒", st == 400)
    lib = [x for x in d.get("assets", []) if x["kind"] == "lib"]
    if lib:
        st, _ = api("DELETE", f"/api/voice_assets/{enc(lib[0]['id'])}")
        check("安全:声音库不可删", st == 400)
        st, _ = api("POST", "/api/voice_assets/rename", {"id": lib[0]["id"], "new_name": "x"})
        check("安全:声音库不可改名", st == 400)
    else:
        check("安全:声音库不可删（空库跳过）", True)
        check("安全:声音库不可改名（空库跳过）", True)

    # ── 7. 孤儿生命周期：删角色 → 孤儿 → bind 找回 → 软删入回收 ──
    api("DELETE", f"/profiles/{enc(P1)}")
    api("DELETE", f"/profiles/{enc(P2)}")
    st, d = api("GET", "/api/voice_assets")
    hits = find_asset(d.get("assets", []), CLONE_NAME + "_renamed")
    check("孤儿:删光引用后 orphan=true", len(hits) == 1 and hits[0].get("orphan") is True)

    api("POST", "/profiles", {"name": P3, "description": "e2e 找回目标"})
    st, r = api("POST", "/api/voice_assets/bind", {"id": a["id"], "profile": P3})
    check("找回:bind 成功", st == 200 and r.get("ok"), extra=str(r))
    st, pr = api("GET", f"/profiles/{enc(P3)}?include_face=false")
    check("找回:角色 has_voice=true", bool(pr.get("has_voice")))
    st, d = api("GET", "/api/voice_assets")
    hits = find_asset(d.get("assets", []), CLONE_NAME + "_renamed")
    check("找回:refs 回算到新角色", hits and hits[0].get("refs") == [P3], extra=str(hits[0].get("refs") if hits else None))

    api("DELETE", f"/profiles/{enc(P3)}")
    st, r = api("DELETE", f"/api/voice_assets/{enc(a['id'])}")
    check("软删:孤儿删除成功", st == 200 and r.get("ok") and r.get("trashed"), extra=str(r.get("trashed")))
    clone_dir = d.get("clone_dir") or ""
    trashed = os.path.join(clone_dir, "_trash", str(r.get("trashed") or ""))
    check("软删:文件进了 _trash", os.path.isfile(trashed), extra=trashed)
    st, d = api("GET", "/api/voice_assets")
    check("软删:列表不再出现", not find_asset(d.get("assets", []), CLONE_NAME))

    # ── 7b 回收站试听（AS8）：还原/彻删前先听一耳朵 ──
    tname = str(r.get("trashed") or "")
    st, audio = api("GET", f"/api/asset_trash/audio?kind=clone&name={enc(tname)}", raw=True)
    check("回收试听:克隆音字节直出", st == 200 and audio[:4] == b"RIFF", extra=f"len={len(audio)}")
    st, _r2 = api("GET", "/api/asset_trash/audio?kind=rvc&name=x.pth")
    check("回收试听:模型拒绝(400)", st == 400)
    st, _r2 = api("GET", "/api/asset_trash/audio?kind=clone&name=" + enc("..\\..\\secrets.wav"))
    check("回收试听:路径穿越被拒(400)", st == 400)

    # ── 7.5 批量清孤儿（AS6）：only 定向清理，被引用文件绝不动 ──
    wav_a, wav_b = make_wav_b64(7.0), make_wav_b64(5.5)   # 两段不同内容 → 哈希必不同
    st, ca = api("POST", "/api/voice_clone",
                 {"wav_base64": wav_a, "name": CLONE_NAME + "pa", "agreed_terms": True, "user_id": "e2e"})
    st, cb = api("POST", "/api/voice_clone",
                 {"wav_base64": wav_b, "name": CLONE_NAME + "pb", "agreed_terms": True, "user_id": "e2e"})
    api("POST", "/profiles", {"name": P3, "voice_b64": ca.get("voice_b64") or "", "description": "e2e 引用保护"})
    st, d = api("GET", "/api/voice_assets")
    mine = find_asset(d.get("assets", []))
    orphans = [x["name"] for x in mine if x["orphan"]]
    bound = [x["name"] for x in mine if not x["orphan"]]
    check("批量清:准备 1 绑定 + 1 孤儿", len(orphans) == 1 and len(bound) == 1,
          extra=f"orphan={orphans} bound={bound}")
    st, r = api("POST", "/api/voice_assets/purge_orphans", {"only": [x["name"] for x in mine]})
    check("批量清:只清走孤儿那条", st == 200 and r.get("moved") == 1 and r.get("names") == orphans,
          extra=str(r))
    st, d = api("GET", "/api/voice_assets")
    left = [x["name"] for x in find_asset(d.get("assets", []))]
    check("批量清:被引用文件原地不动", left == bound, extra=str(left))
    st, t = api("GET", "/api/asset_trash")
    check("批量清:孤儿进统一回收站(kind=clone)",
          any(i["kind"] == "clone" and i["orig_name"] == (orphans[0] if orphans else "?")
              for i in t.get("items", [])))
    st, _ = api("POST", "/api/voice_assets/purge_orphans", {"only": "bad"})
    check("批量清:only 非数组→400", st == 400)

    # ── 7.5b 回收站「清 30 天前」真删语义（AS7）：植入一个 2001 年删除的假回收站文件 ──
    #    仅当机器上没有真实的 ≥30 天旧回收项时才跑真删（避免连带清掉用户数据）
    st, h0 = api("GET", "/api/asset_health")
    trash_dir = os.path.join(clone_dir, "_trash")
    if h0.get("trash_old_n", 0) == 0 and os.path.isdir(os.path.dirname(trash_dir)):
        os.makedirs(trash_dir, exist_ok=True)
        planted = os.path.join(trash_dir, f"1000000000_{CLONE_NAME}_old_plant.wav")
        with open(planted, "wb") as fh:
            fh.write(b"RIFFfakewav")
        st, h1 = api("GET", "/api/asset_health")
        check("清旧:巡检识别 30 天前旧项", h1.get("trash_old_n", 0) == 1
              and h1.get("trash_old_bytes", 0) > 0, extra=str({k: h1.get(k) for k in ("trash_old_n", "trash_old_bytes")}))
        st, r = api("POST", "/api/asset_trash/purge", {"older_than_days": 30})
        check("清旧:只删旧项（planted 1 条）", st == 200 and r.get("purged") == 1, extra=str(r))
        check("清旧:文件确实没了", not os.path.isfile(planted))
        st, h2 = api("GET", "/api/asset_health")
        check("清旧:巡检归零", h2.get("trash_old_n", 0) == 0)
    else:
        print(f"  [SKIP] 机器上已有 {h0.get('trash_old_n')} 条真实旧回收项，跳过真删语义（避免动用户数据）")
        for nm in ("清旧:巡检识别 30 天前旧项（跳过）", "清旧:只删旧项（跳过）",
                   "清旧:文件确实没了（跳过）", "清旧:巡检归零（跳过）"):
            check(nm, True)

    # ── 7.5c 自动清理策略（AS8）：保存即执行——超限时自动删旧项，近期删除保留 ──
    st, h0 = api("GET", "/api/asset_health")
    if h0.get("trash_old_n", 0) == 0 and os.path.isdir(os.path.dirname(trash_dir)):
        os.makedirs(trash_dir, exist_ok=True)
        planted = os.path.join(trash_dir, f"1000000000_{CLONE_NAME}_policy_plant.wav")
        with open(planted, "wb") as fh:
            fh.write(b"R" * (1200 * 1024))          # 1.2MB 旧文件：把回收站顶过 1MB 阈值
        st, r = api("POST", "/api/trash_policy",
                    {"auto_clean": True, "max_mb": 1, "older_days": 30})
        check("策略:保存即执行且只清旧项", st == 200 and r.get("ok") and r.get("cleaned") == 1,
              extra=str(r))
        check("策略:旧文件被自动删除", not os.path.isfile(planted))
        # AS9 透明化：真清了东西 → last_clean_* 即刻更新且随响应返回
        pol = r.get("policy", {})
        check("策略:清理统计已记录", pol.get("last_clean_n") == 1
              and pol.get("last_clean_ts", 0) > time.time() - 60
              and pol.get("total_cleaned", 0) >= 1, extra=str(pol))
        st, g = api("GET", "/api/trash_policy")
        check("策略:配置已持久化", g.get("policy", {}).get("auto_clean") is True
              and g.get("policy", {}).get("max_mb") == 1)
        check("策略:统计已持久化", g.get("policy", {}).get("last_clean_n") == 1)
        st, _r2 = api("POST", "/api/trash_policy", {"max_mb": "abc"})
        check("策略:坏值→400", st == 400)
        st, r = api("POST", "/api/trash_policy",
                    {"auto_clean": False, "max_mb": 500, "older_days": 30})
        check("策略:恢复默认成功", st == 200 and r.get("ok")
              and r.get("policy", {}).get("auto_clean") is False)
    else:
        print("  [SKIP] 机器上已有真实旧回收项，跳过自动清理策略语义")
        for nm in ("策略:保存即执行且只清旧项（跳过）", "策略:旧文件被自动删除（跳过）",
                   "策略:清理统计已记录（跳过）", "策略:配置已持久化（跳过）",
                   "策略:统计已持久化（跳过）", "策略:坏值→400（跳过）", "策略:恢复默认成功（跳过）"):
            check(nm, True)

    # ── 7.5d 批量彻删 items（AS9）：两段式校验——任一非法整批拒绝，合法则全删 ──
    os.makedirs(trash_dir, exist_ok=True)
    b_ts = int(time.time())
    b_names = [f"{b_ts + i}_{CLONE_NAME}_batch{i}.wav" for i in range(2)]
    for nm in b_names:
        with open(os.path.join(trash_dir, nm), "wb") as fh:
            fh.write(b"B")
    st, r = api("POST", "/api/asset_trash/purge", {"items": "bad"})
    check("批量彻删:非数组→400", st == 400)
    st, r = api("POST", "/api/asset_trash/purge",
                {"items": [{"kind": "clone", "name": b_names[0]}, {"kind": "clone", "name": "nope.wav"}]})
    check("批量彻删:含不存在→整批拒(404)", st == 404)
    check("批量彻删:整批拒后文件都还在",
          all(os.path.isfile(os.path.join(trash_dir, nm)) for nm in b_names))
    st, r = api("POST", "/api/asset_trash/purge",
                {"items": [{"kind": "clone", "name": nm} for nm in b_names]})
    check("批量彻删:合法批全删", st == 200 and r.get("purged") == 2, extra=str(r))
    check("批量彻删:文件确实没了",
          not any(os.path.isfile(os.path.join(trash_dir, nm)) for nm in b_names))

    # ── 7.5e 声音链路体检 + 一键修复（AS10）：断链看得见，修复一键达 ──
    VH_P = "_e2e体检角色"
    api("DELETE", f"/profiles/{enc(VH_P)}")
    st, r = api("POST", "/profiles", {"name": VH_P, "description": "as10", "voice_b64": make_wav_b64(4.0)})
    check("体检:建临时角色", st == 200)
    st, r = api("PATCH", f"/profiles/{enc(VH_P)}", {"rvc_model": "_e2e_ghost.pth"})
    check("体检:PATCH 幽灵模型绑定", st == 200)
    st, h = api("GET", "/api/profile_voice_health")
    me = (h.get("profiles") or {}).get(VH_P) or {}
    codes = [i["code"] for i in me.get("issues", [])]
    check("体检:识别无备份+模型断链(level=bad)", st == 200 and me.get("level") == "bad"
          and "voice_unbacked" in codes and "rvc_file_missing" in codes, extra=str(codes))
    check("体检:断链问题带修复指引", all(i.get("fix") for i in me.get("issues", [])))
    st, r = api("POST", "/api/profile_voice_repair", {"name": VH_P, "action": "backup_voice"})
    vh_backup = r.get("file") or ""
    check("体检:一键落盘备份成功", st == 200 and r.get("ok") and vh_backup
          and r.get("existed") is False, extra=vh_backup)
    check("体检:备份文件真实落盘", os.path.isfile(os.path.join(clone_dir, vh_backup)))
    st, r = api("POST", "/api/profile_voice_repair", {"name": VH_P, "action": "backup_voice"})
    check("体检:重复备份幂等返回", st == 200 and r.get("existed") is True and r.get("file") == vh_backup)
    st, r = api("POST", "/api/rvc_assets/bind", {"profile": VH_P, "id": ""})
    check("体检:清除失效绑定(bind空id)", st == 200 and r.get("ok"))
    st, h = api("GET", "/api/profile_voice_health")
    me = (h.get("profiles") or {}).get(VH_P) or {}
    check("体检:修复后链路完整(level=ok)", me.get("level") == "ok" and not me.get("issues"),
          extra=str(me))
    st, _r2 = api("POST", "/api/profile_voice_repair", {"name": VH_P, "action": "hack"})
    check("体检:非法动作→400", st == 400)
    st, _r2 = api("POST", "/api/profile_voice_repair", {"name": "_e2e不存在", "action": "backup_voice"})
    check("体检:角色不存在→404", st == 404)

    # ── 7.5f 素材链路体检扩展（AS11）：形象视频断链 + 清引用修复 + 无备份批量落盘 ──
    st, r = api("PATCH", f"/profiles/{enc(VH_P)}",
                {"idle_video": "_e2e_ghost_idle.mp4", "body_video": "_e2e_ghost_body.mp4"})
    check("形象体检:PATCH 幽灵视频路径", st == 200)
    st, h = api("GET", "/api/profile_voice_health")
    me = (h.get("profiles") or {}).get(VH_P) or {}
    codes = [i["code"] for i in me.get("issues", [])]
    check("形象体检:识别待机+底视频断链(level=bad)", st == 200 and me.get("level") == "bad"
          and "idle_video_missing" in codes and "body_video_missing" in codes, extra=str(codes))
    fixes = {i["code"]: i.get("fix") for i in me.get("issues", [])}
    check("形象体检:断链带清除修复码", fixes.get("idle_video_missing") == "clear_idle_video"
          and fixes.get("body_video_missing") == "clear_body_video")
    st, r = api("POST", "/api/profile_voice_repair", {"name": VH_P, "action": "clear_idle_video"})
    check("形象体检:清除待机视频引用", st == 200 and r.get("cleared") == "idle_video", extra=str(r))
    st, r = api("POST", "/api/profile_voice_repair", {"name": VH_P, "action": "clear_body_video"})
    check("形象体检:清除底视频引用", st == 200 and r.get("cleared") == "body_video")
    st, h = api("GET", "/api/profile_voice_health")
    me = (h.get("profiles") or {}).get(VH_P) or {}
    check("形象体检:清除后链路恢复(level=ok)", me.get("level") == "ok" and not me.get("issues"),
          extra=str(me))
    # 批量落盘：返回逐条结果 → 测后只删「本次新建」的备份文件，精准复原机器状态（不动用户既有备份）
    st, r = api("POST", "/api/profile_voice_repair", {"action": "backup_voice_all"})
    done1 = r.get("done") or []
    mine = next((d0 for d0 in done1 if d0.get("name") == VH_P), None)
    check("批量落盘:整批成功且含测试角色", st == 200 and r.get("ok") and mine is not None,
          extra=str(mine))
    check("批量落盘:已备份角色幂等跳过", bool(mine and mine.get("existed") is True
          and mine.get("file") == vh_backup), extra=str(mine))
    st, r2 = api("POST", "/api/profile_voice_repair", {"action": "backup_voice_all"})
    check("批量落盘:二次调用零新增", st == 200 and r2.get("backed_n") == 0,
          extra=f"backed_n={r2.get('backed_n')}")
    for d0 in done1:
        if d0.get("existed") is False and d0.get("file"):
            fp = os.path.join(clone_dir, d0["file"])
            if os.path.isfile(fp):
                os.remove(fp)

    # ── 7.5g 导入 peek 链路研判（AS12）：绑定的变声模型包里没带、本机也没有 → 导入前亮牌 ──
    api("PATCH", f"/profiles/{enc(VH_P)}", {"rvc_model": "_e2e_ghost.pth"})
    st, vh_pkg = api("GET", f"/api/profile/{enc(VH_P)}/export_package", raw=True)
    check("peek链路:断链角色包可导出", st == 200 and vh_pkg[:2] == b"PK", extra=f"{len(vh_pkg)}B")
    st, pk = api_upload("/api/profile/package_peek", {}, "e2e_vh.zip", vh_pkg)
    check("peek链路:识别 missing（没带且本机没有）", st == 200 and pk.get("rvc_bound") == "_e2e_ghost.pth"
          and pk.get("rvc_link") == "missing",
          extra=str({k: pk.get(k) for k in ("rvc_bound", "rvc_link")}))
    api("POST", "/api/rvc_assets/bind", {"profile": VH_P, "id": ""})

    api("DELETE", f"/profiles/{enc(VH_P)}")
    vb_path = os.path.join(clone_dir, vh_backup)
    if vh_backup and os.path.isfile(vb_path):
        os.remove(vb_path)

    # ── 7.6 配置包 peek（AS6）：明文 / 口令加密 / roundtrip ──
    st, zip_bytes = api("GET", f"/api/profile/{enc(P3)}/export_package", raw=True)
    check("peek:明文包可导出", st == 200 and zip_bytes[:2] == b"PK", extra=f"{len(zip_bytes)}B")
    st, pk = api_upload("/api/profile/package_peek", {}, "e2e.zip", zip_bytes)
    check("peek:明文清单正确", st == 200 and pk.get("ok") and pk.get("profile") == P3
          and pk.get("encrypted") is False and pk.get("has_voice") is True and pk.get("exists") is True,
          extra=str({k: pk.get(k) for k in ("profile", "encrypted", "has_voice", "exists")}))
    check("peek:quality_axes 字段随包返回", isinstance(pk.get("quality_axes"), dict))
    check("peek链路:未绑定模型不给研判", not pk.get("rvc_link"), extra=str(pk.get("rvc_link")))
    st, enc_bytes = api("GET", f"/api/profile/{enc(P3)}/export_package?password=e2epw", raw=True)
    if st == 200 and enc_bytes[:9] == b"AHPKGENC1":
        st, pk = api_upload("/api/profile/package_peek", {}, "e2e.ahpkg", enc_bytes)
        check("peek:加密包不带口令→needs_password", st == 200 and pk.get("needs_password") is True,
              extra=str(pk))
        st, pk = api_upload("/api/profile/package_peek", {"password": "wrong"}, "e2e.ahpkg", enc_bytes)
        check("peek:错口令→400", st == 400)
        st, pk = api_upload("/api/profile/package_peek", {"password": "e2epw"}, "e2e.ahpkg", enc_bytes)
        check("peek:对口令解锁清单", st == 200 and pk.get("ok") and pk.get("profile") == P3
              and pk.get("encrypted") is True and pk.get("needs_password") is False)
        st, r = api_upload("/api/profile/import_package",
                           {"mode": "overwrite", "force": "true", "import_kb": "false", "password": "e2epw"},
                           "e2e.ahpkg", enc_bytes)
        check("peek:加密包带口令导入成功", st == 200 and r.get("ok") and r.get("profile") == P3,
              extra=str(r.get("detail") or r.get("ok")))
    else:
        print(f"  [SKIP] 加密导出不可用（st={st}，可能缺 cryptography），跳过加密 peek 契约")
        for nm in ("peek:加密包不带口令→needs_password（跳过）", "peek:错口令→400（跳过）",
                   "peek:对口令解锁清单（跳过）", "peek:加密包带口令导入成功（跳过）"):
            check(nm, True)

    # ── 8. UI 冒烟：面板渲染 ──
    #   为让面板有确定性内容，再造两条孤儿克隆音（>1 才显「🧹 全部清入回收站」批量按钮）
    st, cd = api("POST", "/api/voice_clone",
                 {"wav_base64": wav, "name": CLONE_NAME, "agreed_terms": True, "user_id": "e2e"})
    api("POST", "/api/voice_clone",
        {"wav_base64": make_wav_b64(6.5), "name": CLONE_NAME + "o2", "agreed_terms": True, "user_id": "e2e"})
    errors = []
    with sync_playwright() as p:
        from playwright.sync_api import Error as PWError
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        # 预写"已看过"标记：首访引导浮层不再与测试竞速（同 voice_binding 的确定性处理）
        pg.add_init_script(
            "try{localStorage.setItem('ah_onboard_v1','1');"
            "localStorage.setItem('avatarhub_seen_tour','1');}catch(_){}")
        pg.on("console", lambda m: errors.append(
            m.text + " @" + ((m.location or {}).get("url") or "")) if m.type == "error" else None)

        def D(expr):
            return pg.evaluate("() => { const d=Alpine.$data(document.body); return " + expr + "; }")

        def ui_smoke():
            pg.goto(HUB + "/ui", wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)   # 留出 hub 刚重启时前端 WS 重连 / 自愈 reload 的窗口
            pg.evaluate("() => { const d=Alpine.$data(document.body); d.onboardShow=false; d.showTour=false; }")
            # 2026-07-07: 面板升级为「资产管理」（声音/变声模型/回收站页签），入口文案 🎛️ 声音 → 🗂️ 资产
            entry_ok = pg.locator("button:has-text('🗂️ 资产')").count() >= 1
            pg.evaluate("() => Alpine.$data(document.body).vaOpen()")
            for _ in range(30):
                if D("!d.vaLoading && d.vaAssets.length>0"):
                    break
                pg.wait_for_timeout(300)
            return entry_ok

        entry_ok = False
        for attempt in (1, 2):   # 页面导航（自动刷新）打断执行上下文 → 整段重试一次
            try:
                entry_ok = ui_smoke()
                break
            except PWError as e:
                if attempt == 2:
                    raise
                print(f"  .. UI 冒烟被页面刷新打断，重试（{e.message.splitlines()[0][:60]}）")
                pg.wait_for_timeout(2000)

        check("UI:入口按钮存在", entry_ok)
        check("UI:面板打开且有数据", D("d.vaShow===true && d.vaAssets.length>0"),
              extra=f"assets={D('d.vaAssets.length')}")
        check("UI:测试克隆条目可见", pg.locator(f"div[role=dialog] >> text={CLONE_NAME}").first.is_visible())
        check("UI:孤儿徽章渲染", pg.locator("span:has-text('孤儿'):visible").count() >= 1)
        check("UI:绑定入口渲染", pg.locator("select[aria-label='绑定到角色']:visible").count() >= 1)
        check("UI:试听按钮渲染", pg.locator("button[aria-label='试听']:visible").count() >= 1)
        # AS6: 批量清孤儿按钮（孤儿>1 才出现）
        check("UI:批量清孤儿按钮可见", pg.locator("button:has-text('全部清入回收站'):visible").count() >= 1)

        # AS6: 导入两步流——选包→预览清单→覆盖预警（不真导入，API 层已验 roundtrip）
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaClose(); "
                    "d.createHubShow=true; d.createHubMode='pkg'; }")
        pg.wait_for_timeout(300)
        pg.set_input_files("input[accept='.zip,.ahpkg']", files=[{
            "name": "e2e_peek.zip", "mimeType": "application/zip", "buffer": zip_bytes}])
        for _ in range(20):
            if D("!!d.pkgPeek && !d.pkgPeekLoading"):
                break
            pg.wait_for_timeout(300)
        check("UI:导入预览卡出现", D("!!d.pkgPeek && d.pkgPeek.profile===" + json.dumps(P3)),
              extra=str(D("d.pkgPeek && d.pkgPeek.profile")))
        check("UI:覆盖预警按钮可见", pg.locator("button:has-text('确认覆盖导入'):visible").count() >= 1)
        check("UI:换包入口可见", pg.locator("button:has-text('换包'):visible").count() >= 1)
        # AS7: 包内质量评分行（注入带分数的 peek → 星标/百分比徽章渲染）
        pg.evaluate("() => { const d=Alpine.$data(document.body); "
                    "d.pkgPeek=Object.assign({}, d.pkgPeek, {quality_axes:{cosine:0.82, naturalness:0.93}}); }")
        pg.wait_for_timeout(300)
        check("UI:质量评分徽章渲染", pg.locator("text=音色贴合 82%").count() >= 1
              and pg.locator("text=自然度 93%").count() >= 1)
        check("UI:金标星号（cos≥0.75）", pg.locator("text=★ 音色贴合 82%").count() >= 1)
        pg.evaluate("() => { const d=Alpine.$data(document.body); "
                    "d.pkgPeek=Object.assign({}, d.pkgPeek, {quality_axes:{cosine:0.42, naturalness:0}}); }")
        pg.wait_for_timeout(250)
        check("UI:低分给闸门预警", pg.locator("text=评分偏低").count() >= 1)
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.pkgReset(); d.createHubShow=false; }")

        # AS7: 回收站「清 30 天前」按钮（注入含旧项的回收站 → 按钮显形）
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaShow=true; d.vaTab='trash'; d.vaLoading=false; "
                    "d.vaTrash=[{kind:'clone',name:'1000000000_x.wav',orig_name:'x.wav',deleted_at:1000000000,size:1024},"
                    "{kind:'clone',name:'9999999999_y.wav',orig_name:'y.wav',deleted_at:Math.floor(Date.now()/1000),size:2048},"
                    "{kind:'rvc',name:'1000000001_m.pth',orig_name:'m.pth',deleted_at:1000000001,size:4096}]; }")
        pg.wait_for_timeout(350)
        check("UI:清 30 天前按钮只认旧项", pg.locator("button:has-text('清 30 天前 (2)'):visible").count() >= 1,
              extra=str(D("d.vaTrashOld && d.vaTrashOld.n")))
        check("UI:清空回收站按钮仍在", pg.locator("button:has-text('清空回收站'):visible").count() >= 1)
        # AS8: 克隆音条目有 ▶ 试听、模型条目没有；自动清理策略行渲染且状态已加载
        check("UI:回收站克隆音可试听", pg.locator("button[aria-label='试听回收站声音']:visible").count() == 2)
        check("UI:策略行渲染", pg.locator("text=🤖 自动清理").count() >= 1
              and pg.locator("input[aria-label='自动清理体积阈值MB']").count() >= 1
              and pg.locator("input[aria-label='自动清理保留天数']").count() >= 1)
        check("UI:策略状态已加载", D("typeof d.trashPolicy.max_mb==='number' && typeof d.trashPolicy.older_days==='number'"),
              extra=str(D("d.trashPolicy")))

        # AS9: 搜索过滤三页签 + 回收站批量勾选 + 策略透明行
        check("UI:搜索框渲染", pg.locator("input[aria-label='搜索资产']:visible").count() >= 1)
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaQuery='y.wav'; }")
        pg.wait_for_timeout(300)
        check("UI:搜索过滤回收站条目", pg.locator("input[aria-label^='选中 ']:visible").count() == 1
              and D("d.vaTrashF.length") == 1, extra=str(D("d.vaTrashF.map(i=>i.orig_name)")))
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaQuery='不存在的名字zz'; }")
        pg.wait_for_timeout(250)
        check("UI:搜索无命中给提示", pg.locator("text=没有匹配").count() >= 1)
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaQuery=''; }")
        pg.wait_for_timeout(250)
        check("UI:回收站条目带勾选框", pg.locator("input[aria-label^='选中 ']:visible").count() == 3)
        # 勾 2 条 → 批量动作接管工具栏；整站动作（清空）让位
        pg.evaluate("() => { const d=Alpine.$data(document.body); "
                    "d.vaSelToggle('t:clone:1000000000_x.wav'); d.vaSelToggle('t:rvc:1000000001_m.pth'); }")
        pg.wait_for_timeout(300)
        check("UI:勾选出批量按钮", pg.locator("button:has-text('还原选中 (2)'):visible").count() >= 1
              and pg.locator("button:has-text('彻删选中 (2)'):visible").count() >= 1)
        check("UI:勾选时清空按钮让位", pg.locator("button:has-text('清空回收站'):visible").count() == 0)
        # 全选=圈中全部 3 条；再点=清空勾选
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaTrashSelAll(); }")
        pg.wait_for_timeout(250)
        check("UI:全选圈中全部", D("d.vaTrashSel.length") == 3)
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaTrashSelAll(); }")
        pg.wait_for_timeout(250)
        check("UI:再点全选=清空", D("d.vaTrashSel.length") == 0)
        # 策略透明行：有 last_clean_ts 才显示
        pg.evaluate("() => { const d=Alpine.$data(document.body); "
                    "d.trashPolicy=Object.assign({}, d.trashPolicy, "
                    "{last_clean_ts:Math.floor(Date.now()/1000)-7200, last_clean_n:3, total_cleaned:9}); }")
        pg.wait_for_timeout(250)
        check("UI:策略透明行可见", pg.locator("text=上次自动清理").count() >= 1
              and pg.locator("text=历史累计 9 项").count() >= 1)
        # 声音页孤儿勾选：注入 1 孤儿 1 在用 → 勾选框只出现在孤儿上，勾中出批量清理按钮
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaTab='voice'; "
                    "d.vaAssets=[{id:'clone:o1.wav',kind:'clone',name:'o1.wav',size:1024,created_at:1700000000,duration_s:3,refs:[],orphan:true},"
                    "{id:'clone:u1.wav',kind:'clone',name:'u1.wav',size:1024,created_at:1700000000,duration_s:3,refs:['角色甲'],orphan:false}]; }")
        pg.wait_for_timeout(350)
        check("UI:孤儿才有勾选框", pg.locator("input[aria-label^='选中孤儿 ']:visible").count() == 1)
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaSelToggle('clone:o1.wav'); }")
        pg.wait_for_timeout(250)
        check("UI:勾孤儿出批量清理按钮", pg.locator("button:has-text('清入回收站 (1)'):visible").count() >= 1,
              extra=str(D("d.vaOrphanSel.length")))
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaSel={}; d.vaShow=false; }")

        # AS10: 声音链路体检——卡片徽标只认真断链；预检黄灯跟随出镜角色；抽屉给一键修复
        orig_active = D("d.active") or ""
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.tab='profiles'; "
                    "d.profiles=[{name:'_e2e断链', has_voice:true, quality_axes:{}, active:false},"
                    "{name:'_e2e无备份', has_voice:true, quality_axes:{}, active:false}]; "
                    "d.voiceHealth={'_e2e断链':{level:'bad', issues:[{code:'rvc_file_missing',sev:'break',fix:'fix_rvc',text:'绑定的变声模型文件已不存在'}]},"
                    "'_e2e无备份':{level:'warn', issues:[{code:'voice_unbacked',sev:'fragile',fix:'backup_voice',text:'磁盘没有备份文件'}]}}; }")
        pg.wait_for_timeout(400)
        # AS12 起徽标是可点的 button（直达修复），定位器同步
        check("UI:断链卡片有创可贴徽标", pg.locator("button:has-text('🩹 素材断链'):visible").count() >= 1)
        check("UI:无备份不上卡片徽标(防狼来了)", pg.locator("button:has-text('🩹 素材断链'):visible").count() == 1)
        pf = pg.evaluate("() => { const d=Alpine.$data(document.body); d.active='_e2e断链'; "
                         "const it=d.preflight().items.find(i=>i.key==='voicelink'); "
                         "return it?(it.status+'|'+it.detail):''; }")
        check("UI:预检出声音链路黄灯", pf.startswith("warn|") and "已不存在" in pf, extra=pf)
        pf2 = pg.evaluate("() => { const d=Alpine.$data(document.body); d.active='_e2e无备份'; "
                          "const it=d.preflight().items.find(i=>i.key==='voicelink'); return it?'has':''; }")
        check("UI:无备份不进预检(正常态)", pf2 == "")
        pg.evaluate("() => { const d=Alpine.$data(document.body); "
                    "d.editP={orig_name:'_e2e断链'}; d.drawerTab='overview'; d.editShow=true; }")
        pg.wait_for_timeout(400)
        check("UI:抽屉断链红条+修复按钮", pg.locator("button:has-text('清除失效绑定'):visible").count() >= 1
              and pg.locator("button:has-text('去换绑模型'):visible").count() >= 1)
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.editP={orig_name:'_e2e无备份'}; }")
        pg.wait_for_timeout(300)
        check("UI:抽屉无备份黄条+落盘按钮", pg.locator("button:has-text('一键落盘备份'):visible").count() >= 1)

        # AS11: 形象断链清引用按钮 / 面板批量落盘按钮 / 换绑智能跳转落点
        pg.evaluate("() => { const d=Alpine.$data(document.body); "
                    "d.voiceHealth['_e2e断链']={level:'bad', issues:[{code:'idle_video_missing',sev:'break',"
                    "fix:'clear_idle_video',text:'待机循环视频 x.mp4 已不存在，待机会回退单图伪活体'}]}; "
                    "d.editP={orig_name:'_e2e断链'}; }")
        pg.wait_for_timeout(300)
        check("UI:形象断链给清视频引用按钮", pg.locator("button:has-text('清除失效视频引用'):visible").count() >= 1)
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.editShow=false; "
                    "d.vaShow=true; d.vaTab='voice'; d.vaLoading=false; "
                    "d.vaEmbedded=[{profile:'甲', matched:false},{profile:'乙', matched:false}]; }")
        pg.wait_for_timeout(300)
        check("UI:无备份出批量落盘按钮", pg.locator("button:has-text('💾 全部落盘'):visible").count() >= 1,
              extra=str(D("d.vaHealth.unbacked")))
        # 智能跳转（打桩 vaRefresh 注入确定性回收站）：被删模型还在回收站 → 直达回收站；不在 → 停模型页签清过滤
        hit = pg.evaluate("async () => { const d=Alpine.$data(document.body); "
                          "d.profiles=[{name:'_e2e断链', rvc_model:'weights/ghost.pth', has_voice:true, quality_axes:{}}]; "
                          "d.vaRefresh=async()=>{ d.vaLoading=false; "
                          "d.vaTrash=[{kind:'rvc',name:'1700000000_ghost.pth',orig_name:'ghost.pth',deleted_at:1700000000,size:1}]; }; "
                          "await d.vhJumpRebind('_e2e断链'); return d.vaTab+'|'+d.vaQuery; }")
        check("UI:换绑跳转-回收站命中直达还原", hit == "trash|ghost", extra=hit)
        miss = pg.evaluate("async () => { const d=Alpine.$data(document.body); "
                           "d.vaRefresh=async()=>{ d.vaLoading=false; d.vaTrash=[]; }; "
                           "await d.vhJumpRebind('_e2e断链'); return d.vaTab+'|'+d.vaQuery; }")
        check("UI:换绑跳转-无备选停模型页", miss == "rvc|", extra=miss)

        # AS12: 搜索态页签命中数——三页签各自实时计数，跨页签命中不用点过去猜
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaShow=true; d.vaLoading=false; d.vaTab='voice'; "
                    "d.vaAssets=[{id:'clone:zz1.wav',kind:'clone',name:'zz1.wav',size:1,created_at:1700000000,duration_s:3,refs:[],orphan:true}]; "
                    "d.vaRvc=[]; d.vaEmbedded=[]; "
                    "d.vaTrash=[{kind:'clone',name:'1700000000_zz1_old.wav',orig_name:'zz1_old.wav',deleted_at:1700000000,size:1}]; "
                    "d.vaQuery='zz1'; }")
        pg.wait_for_timeout(350)
        check("UI:搜索态页签带命中数", pg.locator("button:has-text('🎙️ 声音 (1)'):visible").count() >= 1
              and pg.locator("button:has-text('🎛️ 变声模型 (0)'):visible").count() >= 1
              and pg.locator("button:has-text('♻️ 回收站 (1)'):visible").count() >= 1)
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaQuery=''; }")
        pg.wait_for_timeout(250)
        check("UI:清搜索后声音页签不带计数", pg.locator("button:has-text('🎙️ 声音 (')").count() == 0
              and pg.locator("button:has-text('♻️ 回收站 (1)'):visible").count() >= 1)

        # AS12: 点断链徽标直达抽屉素材链路卡 + 一键全修按钮（≥2 可自动修复项才出现）
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaShow=false; d.tab='profiles'; "
                    "d.profiles=[{name:'_e2e断链', has_voice:true, quality_axes:{}, active:false}]; "
                    "d.voiceHealth={'_e2e断链':{level:'bad', issues:["
                    "{code:'rvc_file_missing',sev:'break',fix:'fix_rvc',text:'绑定的变声模型文件已不存在'},"
                    "{code:'idle_video_missing',sev:'break',fix:'clear_idle_video',text:'待机循环视频已不存在'},"
                    "{code:'voice_unbacked',sev:'fragile',fix:'backup_voice',text:'磁盘没有备份文件'}]}}; }")
        pg.wait_for_timeout(400)
        pg.locator("button:has-text('🩹 素材断链'):visible").first.click()
        pg.wait_for_timeout(700)
        check("UI:点徽标直达抽屉概览", D("d.editShow===true && d.drawerTab==='overview' && d.editP.orig_name==='_e2e断链'"))
        check("UI:一键全修按钮带计数", pg.locator("button:has-text('一键全修 (3)'):visible").count() >= 1)

        # AS12: 导出面板断链预警（break 才预警；fragile 不打扰）
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.editShow=false; "
                    "d.voiceHealth['_e2e好角色']={level:'warn', issues:[{code:'voice_unbacked',sev:'fragile',fix:'backup_voice',text:'磁盘没有备份文件'}]}; "
                    "d.expName='_e2e断链'; d.expShow=true; d.expLoading=false; "
                    "d.expInfo={ok:true, has_face:false, rvc_model:'', est_bytes:{base:1024,face:0,rvc:0}, segments:[], kb_chunks:0}; }")
        pg.wait_for_timeout(350)
        check("UI:导出面板断链预警", pg.locator("text=断链会跟着配置包传播").first.is_visible()
              and pg.locator("button:has-text('先去修复'):visible").count() >= 1)
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.expName='_e2e好角色'; }")
        pg.wait_for_timeout(250)
        check("UI:无断链不出导出预警", not pg.locator("text=断链会跟着配置包传播").first.is_visible())

        # AS12: 导入 peek 链路研判两态（missing 黄牌 / local 绿牌）
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.expShow=false; "
                    "d.createHubShow=true; d.createHubMode='pkg'; d.pkgPeekLoading=false; "
                    "d.pkgPeek={ok:true, profile:'x', exists:false, has_voice:true, has_face:false, has_rvc_model:false, "
                    "segments:0, kb_chunks:0, quality_axes:{}, exported_at:'', encrypted:false, needs_password:false, "
                    "rvc_bound:'ghost.pth', rvc_link:'missing'}; }")
        pg.wait_for_timeout(350)
        check("UI:peek 缺模型黄牌", pg.locator("text=导入后变声不会生效").first.is_visible())
        pg.evaluate("() => { const d=Alpine.$data(document.body); "
                    "d.pkgPeek=Object.assign({}, d.pkgPeek, {rvc_link:'local'}); }")
        pg.wait_for_timeout(250)
        check("UI:peek 本机有模型绿牌", pg.locator("text=本机已有同名模型").first.is_visible()
              and not pg.locator("text=导入后变声不会生效").first.is_visible())
        pg.evaluate(f"() => {{ const d=Alpine.$data(document.body); d.pkgReset(); d.createHubShow=false; d.active={json.dumps(orig_active)}; }}")
        b.close()

    # ── 清理 ──
    cleanup()
    st, d = api("GET", "/api/voice_assets")
    check("清理:无测试残留", not find_asset(d.get("assets", [])))

    ignored = [e for e in errors if "ERR_INVALID_URL" not in e and "data:image/jpeg;base64,undefined" not in e
               and "/profiles/_e2e" not in e]   # 注入的假角色开抽屉会拉详情 404（产线角色卡必是真角色，不会发生）
    check("控制台:无新增 JS 错误", len(ignored) == 0, extra="; ".join(ignored[:3]))

    print(f"\n===== 声音资产 e2e: PASS {len(PASS)}  FAIL {len(FAIL)} =====")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
