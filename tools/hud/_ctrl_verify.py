# -*- coding: utf-8 -*-
"""P2 隔空受控·安全闸逐条红绿 + control_agent 派发单测（无副作用：monkeypatch 注入出口）。
不点真机、不注入真键鼠——只验证「服务端判据」与「事件字典→注入调用」两半各自正确。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"D:\boundless\tools\hud")
import ops_gateway as og  # noqa: E402
import control_agent as ca  # noqa: E402

fails = []


def ck(name, cond):
    print(("  OK  " if cond else " FAIL ") + name)
    if not cond:
        fails.append(name)


# ---- A. 服务端安全闸（arm 拒绝路径；happy-path 需真脸，留真机走查）----
_verdict = {"v": "safe"}
og.configure(get_ctx=lambda: {"verdict": _verdict["v"]}, notify=lambda: None, log_stat=lambda *a, **k: None)

# 确保总闸/熔断洁净
for f in (og.CTRL_DISABLE_FLAG, og.DISABLE_FLAG):
    try:
        f.unlink()
    except OSError:
        pass
og._ctrl_fuse_until = 0.0

r = og.ctrl_arm("zhongshu", src="gesture")            # 非白名单（生产机）
ck("非白名单机(zhongshu)拒", not r["ok"] and "白名单" in r["error"])
r = og.ctrl_arm("yunsheng", src="gesture")            # 生产直播链
ck("非白名单机(yunsheng)拒", not r["ok"] and "白名单" in r["error"])

# 未部署密钥的白名单机 → noagent 拒（真实态；kouxing 尚未部署）
if not og._ctrl_secret("kouxing"):
    r = og.ctrl_arm("kouxing", src="gesture")
    ck("白名单未部署agent拒", not r["ok"] and "未部署" in r["error"])

# 为独立验证 noagent 之后的各闸（auth_src/推流/刷脸），临时假装已部署（monkeypatch 密钥）
_real_secret = og._ctrl_secret
og._ctrl_secret = lambda mid: "testsecret" if mid == "kouxing" else ""

r = og.ctrl_arm("kouxing", src="remote")              # 遥控(非在场信号)
ck("非在场信号(remote)拒", not r["ok"] and "在场" in r["error"])

_verdict["v"] = "danger"                               # 推流硬闸
r = og.ctrl_arm("kouxing", src="gesture")
ck("推流中(danger)拒", not r["ok"] and ("推流" in r["error"] or "冻结" in r["error"]))
_verdict["v"] = "safe"

og.CTRL_DISABLE_FLAG.write_text("x", encoding="utf-8")  # 总闸（在 noagent 之前，无需假密钥也行）
r = og.ctrl_arm("kouxing", src="gesture")
ck("受控总闸开→拒", not r["ok"] and "总闸" in r["error"])
og.CTRL_DISABLE_FLAG.unlink()

# 刷脸 fail-closed：操作员已登记 + 有密钥 + 无帧 → 拒（happy-path 需真脸，此处证「无脸必拒」）
import face_auth  # noqa: E402
if face_auth.enabled():
    r = og.ctrl_arm("kouxing", src="gesture", frame_b64="")
    ck("刷脸层开+无帧→拒(fail-closed)", not r["ok"] and "刷脸" in r["error"])
else:
    print("  --  刷脸层未开（无操作员），跳过")
og._ctrl_secret = _real_secret                         # 还原

# ctrl_action 无会话 → 拒
r = og.ctrl_action("badtoken", "click", {"nx": 0.5, "ny": 0.5}, src="gesture")
ck("无会话动作拒", not r["ok"] and "会话" in r["error"])

# ctrl_pull 密钥错 → 不授予不投递
r = og.ctrl_pull("kouxing", "wrong-secret")
ck("agent 密钥错→不授予空队列", (not r["granted"]) and r["events"] == [])
r = og.ctrl_pull("zhongshu", "whatever")               # 非白名单机 pull
ck("非白名单机 pull→不授予", not r["granted"])

# ---- B. control_agent 派发单测（monkeypatch _send：事件字典→正确注入调用）----
calls = []


def _fake_send(inp):
    calls.append((inp.type, inp.u.mi.dwFlags if inp.type == 0 else inp.u.ki.dwFlags,
                  inp.u.mi.dx if inp.type == 0 else 0,
                  inp.u.mi.dy if inp.type == 0 else 0,
                  inp.u.mi.mouseData if inp.type == 0 else inp.u.ki.wScan))


ca._send = _fake_send  # 唯一系统出口被替换=零真实注入

calls.clear()
ck("click 识别", ca.dispatch({"kind": "click", "nx": 0.5, "ny": 0.5}))
# 期望：move(abs) + leftdown + leftup = 3 次
ck("click=移动+按下+抬起 3 次", len(calls) == 3
   and calls[0][1] & ca.MOUSEEVENTF_ABSOLUTE
   and calls[1][1] == ca.MOUSEEVENTF_LEFTDOWN and calls[2][1] == ca.MOUSEEVENTF_LEFTUP)

calls.clear()
ca.dispatch({"kind": "rclick", "nx": 0.2, "ny": 0.2})
ck("rclick=右键按下+抬起", any(c[1] == ca.MOUSEEVENTF_RIGHTDOWN for c in calls)
   and any(c[1] == ca.MOUSEEVENTF_RIGHTUP for c in calls))

calls.clear()
ca.dispatch({"kind": "dblclick", "nx": 0.4, "ny": 0.4})
downs = sum(1 for c in calls if c[1] == ca.MOUSEEVENTF_LEFTDOWN)
ck("dblclick=两次左键按下", downs == 2)

calls.clear()
ca.dispatch({"kind": "scroll", "nx": 0.5, "ny": 0.5, "dy": -3})
wheel = [c for c in calls if c[1] == ca.MOUSEEVENTF_WHEEL]
# mouseData 是 c_uint32；-3*120=-360 → 补码
ck("scroll=一次滚轮 delta=-360", len(wheel) == 1 and wheel[0][4] == (-360 & 0xFFFFFFFF))

calls.clear()
ca.dispatch({"kind": "drag", "nx": 0.1, "ny": 0.1, "nx2": 0.9, "ny2": 0.9})
ck("drag=按下+移动+抬起含两个绝对点",
   any(c[1] == ca.MOUSEEVENTF_LEFTDOWN for c in calls)
   and any(c[1] == ca.MOUSEEVENTF_LEFTUP for c in calls)
   and sum(1 for c in calls if c[1] & ca.MOUSEEVENTF_ABSOLUTE) >= 2)

calls.clear()
ca.dispatch({"kind": "type", "text": "Ab"})
# 每字符 down+up：2 字符=4 次键盘事件；unicode 标志
kbd = [c for c in calls if c[0] == ca.INPUT_KEYBOARD]
ck("type='Ab'=4 次键盘 unicode 事件", len(kbd) == 4
   and all(c[1] & ca.KEYEVENTF_UNICODE for c in kbd)
   and kbd[0][4] == ord("A") and kbd[2][4] == ord("b"))

calls.clear()
ca.dispatch({"kind": "key", "key": "enter"})
kbd = [c for c in calls if c[0] == ca.INPUT_KEYBOARD]
ck("key=enter=按下+抬起(非 unicode)", len(kbd) == 2 and not (kbd[0][1] & ca.KEYEVENTF_UNICODE))

ck("未知动作返回 False", ca.dispatch({"kind": "nope"}) is False)
ck("坐标钳制 nx=2→1.0 不炸", ca.dispatch({"kind": "move", "nx": 2.0, "ny": -1.0}) is True)

# ---- C. 服务端接合 drill：session→action→agent pull（face 打桩，明示为演练）----
# 证「刷脸通过后，动作确实进队列、且只投给持正确密钥的该机 agent」——把 happy-path 里
# 唯一无法自证的刷脸环节 mock 掉，其余全真跑。
og._ctrl_secret = lambda mid: "drillsecret" if mid == "kouxing" else ""
face_auth.enabled = lambda: True
face_auth.verify = lambda f: (True, "drill", 0.99, "")
og._ctrl = None
og._ctrl_fires = []
r = og.ctrl_arm("kouxing", src="gesture", frame_b64="mockframe")
ck("drill 刷脸通过→拿到 token", r["ok"] and len(r.get("token", "")) >= 16)
tok = r.get("token", "")
r2 = og.ctrl_action(tok, "click", {"nx": 0.42, "ny": 0.66}, src="gesture")
ck("drill 会话内 click 受理", r2["ok"])
# 错密钥 agent 拉：不授予、不投递
rp_bad = og.ctrl_pull("kouxing", "wrong")
ck("drill 错密钥 agent 拉→空", (not rp_bad["granted"]) and rp_bad["events"] == [])
# 正确密钥 agent 拉：授予 + 拿到刚才那条 click（坐标整形后）
rp = og.ctrl_pull("kouxing", "drillsecret")
ck("drill 正确密钥→授予+取到 click",
   rp["granted"] and len(rp["events"]) == 1 and rp["events"][0]["kind"] == "click"
   and abs(rp["events"][0]["nx"] - 0.42) < 1e-6)
# 再拉一次：队列已清空（不重复投递）
rp2 = og.ctrl_pull("kouxing", "drillsecret")
ck("drill 二次拉→队列已清(不重投)", rp2["granted"] and rp2["events"] == [])
# 撤防后：不再授予
og.ctrl_disarm(tok, src="drill")
rp3 = og.ctrl_pull("kouxing", "drillsecret")
ck("drill 撤防后→不授予", not rp3["granted"])

# ---- D. P2b 语音听写路由（voice→ctrl；face 仍打桩为 drill，_tts 打桩免联网）----
import voice_gateway as vg  # noqa: E402
vg._tts = lambda s: ""      # 免联网 fish
vg.configure(ctrl_dictate=og.ctrl_dictate, ctrl_key=og.ctrl_key_active,
             ctrl_active=og.ctrl_active_machine)
og._ctrl = None
og._ctrl_fires = []
r = og.ctrl_arm("kouxing", src="gesture", frame_b64="mock")
ck("P2b 会话就绪", r["ok"])
ck("未武装外的开关：已武装→可开听写", vg.set_dictate(True).get("ok"))
og.ctrl_pull("kouxing", "drillsecret")            # 清队列
vg._handle_dictation("t", "你好世界")
rpd = og.ctrl_pull("kouxing", "drillsecret")
ck("听写文本→远端 type 入队", any(e["kind"] == "type" and e.get("text") == "你好世界" for e in rpd["events"]))
vg._handle_dictation("t", "回车")
rpk = og.ctrl_pull("kouxing", "drillsecret")
ck("导航词「回车」→ key=enter 入队", any(e["kind"] == "key" and e.get("key") == "enter" for e in rpk["events"]))
vg._handle_dictation("t", "往下翻页")
rpp = og.ctrl_pull("kouxing", "drillsecret")
ck("导航词「翻页」→ key=pagedown 入队", any(e.get("key") == "pagedown" for e in rpp["events"]))
vg._handle_dictation("t", "停打字")
ck("「停打字」→ 听写关", not vg._dictate["on"])
og.ctrl_disarm(r["token"], src="drill")
ck("未武装开听写→拒", not vg.set_dictate(True).get("ok"))

print("\n== _ctrl_verify:", "ALL GREEN" if not fails else f"{len(fails)} FAIL: {fails}", "==")
sys.exit(1 if fails else 0)
