# -*- coding: utf-8 -*-
"""人设工作室抽屉「克隆登记 / 复用已有音色」cp-voice mode="enroll" 档 真浏览器门禁
（Playwright 夹具；2026-09-19 随「陈美玲→Claire 复用后文字闪一下就没」修复落地）。

**为什么需要它**：事故根因不在组件内部，而在**宿主与组件的握手时序**——复制成功 →
组件广播 cp-voice-rebound → 抽屉回灌人设 → peVcSync 重新赋 `host.client = c` →
组件 `set client` 无条件整段 innerHTML 重渲 → 成功提示 / 下拉选择 / 试听在 200ms 内
一起归零。tools/verify_cp_voice_ui.py 只压会话档（生成/发送状态机），登记档与「宿主
反复同步」这条路径此前没有任何真浏览器覆盖；静态门禁只能证「字符串在」。

**夹具模式**（与 verify_cp_voice_ui.py 同族）：file:// 自包含页面 + 真组件 + 真词典
（CP_LANG 钉 zh）+ stub client——组件全部 IO 走注入的 client（listPersonas /
voiceProfiles / voiceRebind / voiceTts / voiceEnroll / voiceReconcile），零实例依赖、
零生产写入。`hostSync(pid)` = 人设页 peVcSync 的原样动作（setAttribute('persona') +
再赋同一个 client），用它模拟宿主每一次同步。

覆盖的不变量（编号对应 run() 里的断言）：
  E1  初始渲染：登记目标 / 复用目标预选宿主给的人设、显示名预填、源下拉只列有声人设；
      形态标记以 /api/voice/profiles 为准——🎤 克隆就绪 / 🔊 预置声 / 🎤⚠ 克隆未就绪
      （源下拉置灰）；图例行在
  E2  宿主同 client 重复同步 → **不整体重渲**（enroll 容器身份不变），选择与显示名保留，
      但下拉标记确实软刷新了（listPersonas 被再次调用）
  E3  未选源 / 源=目标 → 提示「请选择…」且不发请求
  E4  复制成功：先「复制中…」后 ✅ 带源人设名；请求体 from/to 正确；广播 cp-voice-rebound
      （detail.from_name/to_persona_id）；下拉选择保留；目标人设标记 🔊→🎤；试听按目标人设合成
  E5  **事故形态**：成功后宿主回灌触发 hostSync → 提示、试听、下拉选择全部存活，容器不重渲
  E6  复制失败（服务端 message）→ ❌ + 原因；不广播；选择保留
  E7  复制请求抛错（网络）→ ❌ + 错误文本
  E8  宿主换人设 → 目标下拉跟随、显示名跟随（自动预填值）、上一位的提示 / 试听清空、
      源=新目标时源清空；容器不重渲
  E9  换回原人设 → 显示名跟随回来
  E10 人手改过的显示名换人设不被覆盖；清空后再换人设恢复跟随
  E11 登记成功 ✅ 同样在宿主同步后存活（与复用同一条被抹的路径），事件 cp-voice-enrolled 带 persona_id，
      目标人设标记随登记变 🎤
  E12 换成**另一个** client 实例 → 才整体重渲（守卫是「同 client」不是「永不」）
  E13 /api/voice/profiles 拉不到（旧后端 / 壳未暴露）→ 标记退回 summary.voice_mode → has_voice 旧口径，
      源下拉不置灰（没有就绪度信息就不替人做决定）

用法::

    python tools/verify_cp_voice_enroll_ui.py             # 门禁模式
    python tools/verify_cp_voice_enroll_ui.py --headed    # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>cp-voice enroll probe</title></head>
<body style="margin:16px;font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;max-width:420px;">
<script>
window.CP_LANG = 'zh';   // 词典语言钉死，断言与坐席实际看到的中文一致
</script>
<script src="@@I18N_JS@@"></script>
<script>
window.__calls = { list: 0, profiles: 0, rebind: [], tts: [], enroll: [], reconcile: 0 };
// 服务端状态（可变）：复制/登记成功后目标人设变成克隆声，下拉标记应跟着变
window.__state = {
  summary: [
    { id: 'meiling', name: '陈美玲', has_voice: true,  voice_mode: '' },        // 存量克隆档：无显式 voice_mode
    { id: 'claire',  name: 'Claire', has_voice: true,  voice_mode: 'preset' },  // 预置声
    { id: 'nova',    name: 'Nova',   has_voice: false, voice_mode: 'off' },     // 不发语音
    { id: 'broken',  name: 'Broken', has_voice: true,  voice_mode: 'clone' },   // 克隆但未就绪
    { id: 'sora',    name: 'Sora',   has_voice: true,  voice_mode: 'preset' },  // 预置声但 profiles 里没有
  ],
  profiles: [
    { persona_id: 'meiling', name: '陈美玲', is_clone: true,  ready: true },
    { persona_id: 'claire',  name: 'Claire', is_clone: false, ready: true },
    { persona_id: 'broken',  name: 'Broken', is_clone: true,  ready: false },
  ],
};
window.__rebindResp = null;      // null=按服务端语义成功；对象=原样返回（失败态）
window.__rebindThrow = false;    // true=请求抛错（网络）
window.__rebindDelayMs = 80;
window.__ttsDelayMs = 30;
window.__profilesFail = false;   // true=/api/voice/profiles 不可用（旧后端 / 壳未暴露）
const clone = (o) => JSON.parse(JSON.stringify(o));
window.__mkClient = () => ({
  async listPersonas() { window.__calls.list++; return { summary: clone(window.__state.summary) }; },
  async voiceProfiles() {
    window.__calls.profiles++;
    if (window.__profilesFail) return { ok: false };
    return { ok: true, default: {}, profiles: clone(window.__state.profiles) };
  },
  async voiceRebind(body) {
    window.__calls.rebind.push(clone(body || {}));
    await new Promise((r) => setTimeout(r, window.__rebindDelayMs));
    if (window.__rebindThrow) throw new Error('net-down');
    if (window.__rebindResp) return clone(window.__rebindResp);
    const to = String((body || {}).to_persona_id || '');
    const s = window.__state.summary.find((x) => x.id === to);
    if (s) { s.has_voice = true; s.voice_mode = 'clone'; }
    const p = window.__state.profiles.find((x) => x.persona_id === to);
    if (p) { p.is_clone = true; p.ready = true; }
    else window.__state.profiles.push({ persona_id: to, name: to, is_clone: true, ready: true });
    return { ok: true, from_persona_id: (body || {}).from_persona_id, to_persona_id: to };
  },
  async voiceTts(args) {
    window.__calls.tts.push(clone(args || {}));
    await new Promise((r) => setTimeout(r, window.__ttsDelayMs));
    return { ok: true, audio_url: 'data:audio/mp3;base64,AAAA',
      voice_meta: { provider: 'avatar_clone', persona_id: (args || {}).persona_id || '' } };
  },
  async voiceEnroll(p) {
    p = p || {};
    window.__calls.enroll.push({ persona_id: p.persona_id, preferred_name: p.preferred_name, hasFile: !!p.file });
    await new Promise((r) => setTimeout(r, 30));
    const s = window.__state.summary.find((x) => x.id === p.persona_id);
    if (s) { s.has_voice = true; s.voice_mode = 'clone'; }
    const pr = window.__state.profiles.find((x) => x.persona_id === p.persona_id);
    if (pr) { pr.is_clone = true; pr.ready = true; }
    else window.__state.profiles.push({ persona_id: p.persona_id, name: p.persona_id, is_clone: true, ready: true });
    return { ok: true, mode: 'zeroshot', reference_audio_path: 'config/voice_refs/x.wav', quality: {} };
  },
  async voiceReconcile() {
    window.__calls.reconcile++;
    return { summary: { cloud_total: 0, local_voice_ids: 0, orphan_count: 0 }, orphans: [], dangling: [] };
  },
});
window.__client = window.__mkClient();
window.__events = [];
['cp-voice-rebound', 'cp-voice-enrolled', 'cp-voice-enroll-failed'].forEach((n) => {
  document.addEventListener(n, (ev) => window.__events.push({ type: n, detail: clone((ev && ev.detail) || {}) }));
});
</script>
<script src="@@VOICE_JS@@"></script>
<script>
const el = document.createElement('cp-voice');
el.setAttribute('mode', 'enroll');
el.setAttribute('persona', 'claire');
document.body.appendChild(el);
el.client = window.__client;     // 宿主 peVcSync 顺序：先 setAttribute(persona) 再赋 client
window.__el = el;
window.q = (sel) => window.__el.shadowRoot.querySelector(sel);
window.qa = (sel) => Array.from(window.__el.shadowRoot.querySelectorAll(sel));
window.T = (k, v) => window.CopilotShared.t(k, v);
// = personas.html peVcSync：同一个 client 反复赋值（每次抽屉同步 / 回灌都会来一次）
window.hostSync = (pid) => { window.__el.setAttribute('persona', pid); window.__el.client = window.__client; };
window.enrollNodeId = () => {
  const n = window.q('[data-role="enroll"]');
  if (!n) return '';
  if (!n.__id) n.__id = 'n' + Math.random().toString(36).slice(2);
  return n.__id;
};
window.opts = (role) => window.qa('[data-role="' + role + '"] option').map((o) => ({ v: o.value, t: o.textContent, d: o.disabled }));
window.snap = () => ({
  enrollId: window.enrollNodeId(),
  epersona: (window.q('[data-role="epersona"]') || {}).value || '',
  ename: (window.q('[data-role="ename"]') || {}).value || '',
  rfrom: (window.q('[data-role="rfrom"]') || {}).value || '',
  rto: (window.q('[data-role="rto"]') || {}).value || '',
  rhint: (window.q('[data-role="rhint"]') || {}).textContent || '',
  ehint: (window.q('[data-role="ehint"]') || {}).textContent || '',
  legend: (window.q('[data-role="rlegend"]') || {}).textContent || '',
  audition: (window.q('[data-role="audition"]') || {}).textContent || '',
  hasAudio: !!window.q('[data-role="audition"] audio'),
  rfromOpts: window.opts('rfrom'), rtoOpts: window.opts('rto'), epOpts: window.opts('epersona'),
  calls: { list: window.__calls.list, profiles: window.__calls.profiles, rebind: window.__calls.rebind.length,
           tts: window.__calls.tts.length, enroll: window.__calls.enroll.length },
  events: window.__events.map((e) => e.type),
});
window.waitFor = async (fn, ms) => {
  const t0 = Date.now();
  while (Date.now() - t0 < (ms || 3000)) {
    try { if (fn()) return true; } catch (_e) { /* */ }
    await new Promise((r) => setTimeout(r, 20));
  }
  return false;
};
window.setSel = (role, v) => { const s = window.q('[data-role="' + role + '"]'); s.value = v; return s.value === v; };
</script>
</body></html>
"""


def build_fixture_page(tmp: Path) -> Path:
    html = (_HTML
            .replace("@@I18N_JS@@", (ENGINE / "shared/copilot/i18n/cp-i18n.js").as_uri())
            .replace("@@VOICE_JS@@", (ENGINE / "shared/copilot/components/cp-voice.js").as_uri()))
    fp = tmp / "probe_voice_enroll.html"
    fp.write_text(html, encoding="utf-8")
    return fp


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        okk = bool(cond)
        self.results.append((name, okk))
        print(f"  [{'PASS' if okk else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return okk

    def summary(self) -> int:
        fails = [n for n, okk in self.results if not okk]
        total = len(self.results)
        print(f"\n== cp-voice 登记档/复用 验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def _opt(opts: List[dict], v: str) -> dict:
    for o in opts:
        if o["v"] == v:
            return o
    return {"v": v, "t": "<missing>", "d": None}


def run(page, ck: Checker) -> None:
    ev = page.evaluate
    ok_txt = ev("T('cp.voice.rebind_ok', {from: '陈美玲'})")
    busy_txt = ev("T('cp.voice.rebind_busy')")
    pick_txt = ev("T('cp.voice.rebind_pick')")
    enroll_hint = ev("T('cp.voice.enroll_hint')")
    enroll_ok = ev("T('cp.voice.enroll_ok')")

    # E1 初始渲染
    ev("waitFor(() => qa('[data-role=\"rfrom\"] option').length > 1 && (q('[data-role=\"ename\"]')||{}).value)")
    s = ev("snap()")
    ck.check("E1 登记目标/复用目标预选宿主人设、显示名预填",
             s["epersona"] == "claire" and s["rto"] == "claire" and s["ename"] == "Claire" and s["rfrom"] == "",
             f"ep={s['epersona']} rto={s['rto']} ename={s['ename']!r}")
    ck.check("E1 源下拉只列有声人设（无 Nova）",
             [o["v"] for o in s["rfromOpts"]] == ["", "meiling", "claire", "broken", "sora"],
             str([o["v"] for o in s["rfromOpts"]]))
    ck.check("E1 形态标记：🎤 克隆就绪 / 🔊 预置 / 🎤⚠ 未就绪 / profiles 缺席的有声人设按 voice_mode",
             _opt(s["rfromOpts"], "meiling")["t"] == "陈美玲 🎤"
             and _opt(s["rfromOpts"], "claire")["t"] == "Claire 🔊"
             and _opt(s["rfromOpts"], "broken")["t"] == "Broken 🎤⚠"
             and _opt(s["rfromOpts"], "sora")["t"] == "Sora 🔊"
             and _opt(s["epOpts"], "nova")["t"] == "Nova",
             str([o["t"] for o in s["rfromOpts"]]))
    ck.check("E1 未就绪克隆声在源下拉置灰，在目标下拉可选",
             _opt(s["rfromOpts"], "broken")["d"] is True and _opt(s["rtoOpts"], "broken")["d"] is False
             and _opt(s["epOpts"], "broken")["d"] is False)
    ck.check("E1 图例行在（🎤/🔊/⚠ 三义）", "🎤" in s["legend"] and "🔊" in s["legend"] and "⚠" in s["legend"], s["legend"])

    # E2 宿主同 client 重复同步 → 软刷新，不整体重渲
    before = s
    ev("hostSync('claire')")
    ev(f"waitFor(() => __calls.list > {before['calls']['list']})")
    page.wait_for_timeout(120)
    s = ev("snap()")
    ck.check("E2 同 client 重复同步不整体重渲（enroll 容器身份不变）", s["enrollId"] == before["enrollId"])
    ck.check("E2 软刷新确实发生（listPersonas 再调）且选择/显示名保留",
             s["calls"]["list"] > before["calls"]["list"] and s["epersona"] == "claire"
             and s["rto"] == "claire" and s["ename"] == "Claire")

    # E3 未选源 / 源=目标
    ev("() => { q('[data-act=\"rebind\"]').click(); }")
    s = ev("snap()")
    ck.check("E3 未选源 → 提示且不发请求", s["rhint"] == pick_txt and s["calls"]["rebind"] == 0, s["rhint"])
    ev("() => { setSel('rfrom', 'claire'); q('[data-act=\"rebind\"]').click(); }")
    s = ev("snap()")
    ck.check("E3 源=目标 → 提示且不发请求", s["rhint"] == pick_txt and s["calls"]["rebind"] == 0)

    # E4 复制成功
    ev("() => { setSel('rfrom', 'meiling'); q('[data-act=\"rebind\"]').click(); }")
    s = ev("snap()")
    ck.check("E4 点复制立即「复制中…」", s["rhint"] == busy_txt, s["rhint"])
    got = ev(f"waitFor(() => (q('[data-role=\"rhint\"]')||{{}}).textContent === {ok_txt!r})")
    ck.check("E4 成功提示 ✅ 带源人设名", bool(got), ev("snap()")["rhint"])
    body = ev("window.__calls.rebind[0]")
    ck.check("E4 请求体 from/to 正确",
             body.get("from_persona_id") == "meiling" and body.get("to_persona_id") == "claire", str(body))
    ev("waitFor(() => __events.some(e => e.type === 'cp-voice-rebound'))")
    det = ev("(window.__events.find(e => e.type === 'cp-voice-rebound') || {}).detail")
    ck.check("E4 广播 cp-voice-rebound（from_name/to_persona_id）",
             det and det.get("from_persona_id") == "meiling" and det.get("from_name") == "陈美玲"
             and det.get("to_persona_id") == "claire", str(det))
    ev("waitFor(() => !!q('[data-role=\"audition\"] audio'))")
    s = ev("snap()")
    ck.check("E4 下拉选择保留（源=陈美玲 / 目标=Claire / 登记目标=Claire）",
             s["rfrom"] == "meiling" and s["rto"] == "claire" and s["epersona"] == "claire",
             f"rfrom={s['rfrom']} rto={s['rto']} ep={s['epersona']}")
    ck.check("E4 目标人设标记 🔊→🎤（复制结果可见）",
             _opt(s["rtoOpts"], "claire")["t"] == "Claire 🎤" and _opt(s["epOpts"], "claire")["t"] == "Claire 🎤",
             _opt(s["rtoOpts"], "claire")["t"])
    tts = ev("window.__calls.tts[window.__calls.tts.length-1]")
    ck.check("E4 试听按目标人设合成", s["hasAudio"] and tts and tts.get("persona_id") == "claire", str(tts))

    # E5 事故形态：成功后宿主回灌 → hostSync
    before = s
    ev("hostSync('claire')")
    ev(f"waitFor(() => __calls.list > {before['calls']['list']})")
    page.wait_for_timeout(400)
    s = ev("snap()")
    ck.check("E5 宿主回灌后成功提示仍在（事故：200ms 后被整体重渲抹掉）", s["rhint"] == ok_txt, s["rhint"])
    ck.check("E5 试听与下拉选择存活、容器不重渲",
             s["hasAudio"] and s["rfrom"] == "meiling" and s["rto"] == "claire" and s["enrollId"] == before["enrollId"],
             f"audio={s['hasAudio']} rfrom={s['rfrom']} sameNode={s['enrollId'] == before['enrollId']}")

    # E6 复制失败（服务端 message）
    ev("() => { window.__rebindResp = { ok: false, message: 'src-no-voice' }; q('[data-act=\"rebind\"]').click(); }")
    ev("waitFor(() => /src-no-voice/.test((q('[data-role=\"rhint\"]')||{}).textContent || ''))")
    s = ev("snap()")
    ck.check("E6 失败 → ❌ + 服务端原因", s["rhint"].startswith("❌") and "src-no-voice" in s["rhint"], s["rhint"])
    ck.check("E6 失败不广播、选择保留",
             s["events"].count("cp-voice-rebound") == 1 and s["rfrom"] == "meiling" and s["rto"] == "claire")

    # E7 请求抛错
    ev("() => { window.__rebindResp = null; window.__rebindThrow = true; q('[data-act=\"rebind\"]').click(); }")
    ev("waitFor(() => /net-down/.test((q('[data-role=\"rhint\"]')||{}).textContent || ''))")
    s = ev("snap()")
    ck.check("E7 请求抛错 → ❌ + 错误文本", s["rhint"].startswith("❌") and "net-down" in s["rhint"], s["rhint"])
    ev("() => { window.__rebindThrow = false; }")

    # E8 宿主换人设
    before = s
    ev("hostSync('meiling')")
    ev("waitFor(() => (q('[data-role=\"epersona\"]')||{}).value === 'meiling')")
    page.wait_for_timeout(120)
    s = ev("snap()")
    ck.check("E8 换人设：目标下拉跟随、显示名跟随",
             s["epersona"] == "meiling" and s["rto"] == "meiling" and s["ename"] == "陈美玲",
             f"ep={s['epersona']} rto={s['rto']} ename={s['ename']!r}")
    ck.check("E8 上一位的提示/试听清空、源=新目标时源清空",
             s["rhint"] == "" and (not s["hasAudio"]) and s["audition"] == "" and s["rfrom"] == ""
             and s["ehint"] == enroll_hint,
             f"rhint={s['rhint']!r} audio={s['hasAudio']} rfrom={s['rfrom']!r}")
    ck.check("E8 换人设不整体重渲", s["enrollId"] == before["enrollId"])

    # E9 换回
    ev("hostSync('claire')")
    ev("waitFor(() => (q('[data-role=\"epersona\"]')||{}).value === 'claire')")
    s = ev("snap()")
    ck.check("E9 换回原人设显示名跟随", s["ename"] == "Claire" and s["rto"] == "claire", s["ename"])

    # E10 人手改过的显示名不被覆盖
    ev("() => { q('[data-role=\"ename\"]').value = 'Custom Voice'; }")
    ev("hostSync('meiling')")
    ev("waitFor(() => (q('[data-role=\"epersona\"]')||{}).value === 'meiling')")
    s = ev("snap()")
    ck.check("E10 人手改过的显示名换人设不覆盖", s["ename"] == "Custom Voice", s["ename"])
    ev("() => { q('[data-role=\"ename\"]').value = ''; }")
    ev("hostSync('claire')")
    ev("waitFor(() => (q('[data-role=\"ename\"]')||{}).value === 'Claire')")
    s = ev("snap()")
    ck.check("E10 清空后再换人设恢复跟随", s["ename"] == "Claire" and s["epersona"] == "claire", s["ename"])

    # E11 登记成功 ✅ 在宿主同步后存活
    ev("hostSync('nova')")
    ev("waitFor(() => (q('[data-role=\"epersona\"]')||{}).value === 'nova')")
    page.set_input_files('cp-voice [data-role="efile"]',
                         {"name": "ref.wav", "mimeType": "audio/wav", "buffer": b"RIFF\x24\x00\x00\x00WAVEfmt "})
    ev("() => { q('[data-role=\"econsent\"]').checked = true; q('[data-act=\"enroll-submit\"]').click(); }")
    ev(f"waitFor(() => ((q('[data-role=\"ehint\"]')||{{}}).textContent || '').indexOf({enroll_ok!r}) === 0)")
    s = ev("snap()")
    ck.check("E11 登记成功 ✅", s["ehint"].startswith(enroll_ok) and s["calls"]["enroll"] == 1, s["ehint"])
    det = ev("(window.__events.find(e => e.type === 'cp-voice-enrolled') || {}).detail")
    ck.check("E11 广播 cp-voice-enrolled 带 persona_id", det and det.get("persona_id") == "nova", str(det))
    ev("waitFor(() => !!q('[data-role=\"audition\"] audio'))")
    s = ev("snap()")
    ck.check("E11 登记后目标人设标记变 🎤、登记目标选择保留",
             _opt(s["epOpts"], "nova")["t"] == "Nova 🎤" and s["epersona"] == "nova", _opt(s["epOpts"], "nova")["t"])
    before = s
    ev("hostSync('nova')")
    ev(f"waitFor(() => __calls.list > {before['calls']['list']})")
    page.wait_for_timeout(400)
    s = ev("snap()")
    ck.check("E11 宿主同步后登记成功提示与试听仍在", s["ehint"].startswith(enroll_ok) and s["hasAudio"]
             and s["enrollId"] == before["enrollId"], s["ehint"])

    # E12 换 client 实例 → 才整体重渲
    before = s
    ev("() => { window.__client = window.__mkClient(); window.__el.client = window.__client; }")
    ev("waitFor(() => enrollNodeId() !== '' && enrollNodeId() !== " + repr(before["enrollId"]) + " && qa('[data-role=\"rfrom\"] option').length > 1)")
    s = ev("snap()")
    ck.check("E12 换 client 实例整体重渲（守卫是同 client，不是永不）",
             s["enrollId"] != before["enrollId"] and s["epersona"] == "nova" and s["rto"] == "nova",
             f"same={s['enrollId'] == before['enrollId']} ep={s['epersona']}")

    # E13 profiles 不可用 → 退回 voice_mode / has_voice 口径，源下拉不置灰
    ev("""() => {
      window.__profilesFail = true;
      const e2 = document.createElement('cp-voice');
      e2.setAttribute('mode', 'enroll'); e2.setAttribute('persona', 'claire');
      document.body.appendChild(e2);
      e2.client = window.__mkClient();
      window.__el2 = e2;
    }""")
    ev("waitFor(() => window.__el2.shadowRoot.querySelectorAll('[data-role=\"rfrom\"] option').length > 1)")
    o13 = ev("""() => Array.from(window.__el2.shadowRoot.querySelectorAll('[data-role="rfrom"] option'))
                 .map(o => ({ v: o.value, t: o.textContent, d: o.disabled }))""")
    ck.check("E13 profiles 缺席：voice_mode=clone→🎤、preset→🔊、无 voice_mode 的有声→🎤（旧口径）、不置灰",
             _opt(o13, "meiling")["t"] == "陈美玲 🎤" and _opt(o13, "sora")["t"] == "Sora 🔊"
             and _opt(o13, "broken")["t"] == "Broken 🎤" and _opt(o13, "broken")["d"] is False,
             str([(o["t"], o["d"]) for o in o13]))
    ev("() => { window.__profilesFail = false; }")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装，跳过（exit 0）")
        return 0
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with tempfile.TemporaryDirectory() as td:
        page_fp = build_fixture_page(Path(td))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 520, "height": 900})
            errors: List[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(page_fp.as_uri())
            page.wait_for_timeout(150)   # 组件注册 + 首次渲染
            run(page, ck)
            ck.check("E0 全程零未捕获 JS 异常", not errors, "; ".join(errors[:3]))
            browser.close()
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
