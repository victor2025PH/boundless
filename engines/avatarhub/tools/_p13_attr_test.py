# -*- coding: utf-8 -*-
"""_interp_silence_attribution 归因分类逻辑测试(桩掉 requests)。"""
import os, sys, types, re

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "avatar_hub.py")
with open(SRC, "r", encoding="utf-8") as f:
    src = f.read()
m = re.search(r"def _interp_silence_attribution.*?(?=\nasync def _cable_silence_tick)", src, re.S)
assert m, "函数未找到"

class _FakeResp:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p

PAYLOAD = {}
fake_requests = types.SimpleNamespace(get=lambda *a, **k: _FakeResp(PAYLOAD))
G = {"os": os, "requests": fake_requests}
exec(m.group(0), G)
attr = G["_interp_silence_attribution"]

fails = []
def case(name, payload, expect_substr):
    global PAYLOAD
    PAYLOAD = payload
    out = attr()
    ok = (expect_substr in out) if expect_substr else (out == "")
    print(("PASS " if ok else f"FAIL({out[:60]}) ") + name)
    if not ok: fails.append(name)

case("未运行→空(保通用文案)", {"running": False}, "")
case("声纹拦截主导", {"running": True, "voicelock": {"thr": 0.4, "last_sim": 0.19},
                    "drops": {"spk": 8}, "fin_a": 1, "stats": {"a": 1}}, "声纹锁正在拦截")
case("影子模式提示", {"running": True, "voicelock": {"shadow": True}, "drops": {"spk": 9}}, "影子模式")
case("整场无识别", {"running": True, "voicelock": {}, "drops": {"gate": 7}, "stats": {}}, "无有效识别")
case("无音色样本", {"running": True, "voicelock": {}, "voice_ok": False,
                  "drops": {"spk": 1}, "fin_a": 6, "stats": {"a": 6}}, "无音色样本")
case("链路正常疑设备漂移", {"running": True, "voicelock": {}, "voice_ok": True,
                        "drops": {}, "fin_a": 12, "stats": {"a": 12}}, "试音")
case("正常对话偶有旁人拦截→不误归因声纹", {"running": True, "voicelock": {}, "voice_ok": True,
                        "drops": {"spk": 2}, "fin_a": 30, "stats": {"a": 30}}, "试音")
print("\n" + ("ALL PASS" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
