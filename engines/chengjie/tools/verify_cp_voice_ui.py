# -*- coding: utf-8 -*-
"""右栏「语音克隆/发送」cp-voice 状态机 真浏览器门禁（Playwright；
P0 2026-08-05 随「预览清不掉 / 生成入口迷失 / 连点双发」修复落地）。

**为什么需要它**：坐席实录两连抱怨——生成过的试听赖在面板里清不掉、想再来一条
找不到生成按钮；代码层根因是组件没有状态机（预览只在切会话时随整块重渲染消失、
发送成功不复位、请求期按钮不禁用、发送链没有幂等键）。这些全是**组件内交互时序**，
静态门禁只能证「字符串在」，证不了「点了会怎样」，只有真浏览器能压。

**夹具模式**（与 tools/verify_goal_form_ui.py 同族）：file:// 自包含页面 +
真组件（cp-voice.js）+ 真词典（cp-i18n.js，CP_LANG 钉 zh）+ **stub client 对象**
——cp-voice 全部 IO 走注入的 client（voiceTts/sendVoice/voiceProfiles），
连假 fetch 都不用，零实例依赖、零生产写入、不烧一分钱 TTS。

覆盖的不变量（编号对应 run() 里的断言）：
  V1  初始渲染：生成按钮是动词「生成语音」，预览区隐藏
  V2  空文本点生成 → 红字提示「请先输入文字」，不发请求
  V3  生成：请求期按钮禁用+「生成中…」；完成出预览（音频/重新生成/✕/发送），
      voiceTts 带会话上下文（chat_key/platform/account_id，试听=发送契约不回退）
  V4  生成在途连点 → 只发一次合成请求
  V5  ✕ 清除 → 预览隐藏且清空，文字不动（报障①）
  V6  预览区「重新生成」→ 再发一次合成（报障②：入口常驻可见）
  V7  改文字 → 预览标过期（stale 边框+警示行+发送禁用）；改回原文 → 自动解除
  V8  换音色 → 同样过期；换回 → 解除（发送按当前音色重合成，防「听A发B」）
  V9  发送成功 → 预览+文字全复位、提示「已发送」、cp-voice-sent 冒泡（detail.reused
      分桶）、幂等键 client_msg_id=cpv-* 与 preview_filename（所听即所发）随请求；
      提示驻留后自动恢复默认引导
  V10 发送在途连点 → 只发一次（防双发给客户）
  V11 发送失败 → 预览保留、发送按钮恢复可点、错误文案透传服务端 message
  V12 生成失败 → 错误态自带「重试/✕」（错误文案同样可清除）
  V13 回落警示：voice_meta.fallback_from 非空 → 「标准音色」黄字警示可见
  V14 cp-fill（草稿填入）程序化改文字 → 同样触发过期标记
  V15 生成在途切会话 → 结果作废不回写新会话（epoch 代际防串）
  V16 生成可取消（代际作废复用同一机制）：立即复位、在途结果静默丢弃
  V17 字数计 400（与服务端试听上限同口径）：超限红字+生成禁用，回限自动恢复
  V18 译声（P0-V2b 2026-08-30）：默认 target_lang='auto' 跟随会话客户语言、
      预览渲染译稿行（所见即所念）、开关翻转=预览过期且请求不再带目标语、
      发送事件带 translated 分桶、cp-voice-xl-changed 广播给宿主
  V19 宿主目标语偏好（P0-V2c）：__cpVoiceXlPref 显式语种覆盖 auto（坐席
      「我的消息 → X」心智模型）、预告行即时显示、偏好换语言=预览过期
  V21 回落原因分支（P0 2026-08-31 提示风暴复盘）：lang_unsupported 走 info
      蓝条（刻意保护非故障）+「改发原文」一键出路（关跟随+自动重生成）+
      顶部语种预告同因去重；通道故障才走「暂不可用」黄条
  V22 降级发送显式确认：非克隆声发送前 confirm（取消=不发/确认=发/同会话
      记住选择），「回落必须显式」不变量的强化承接
  V23 疑似无声=真闸门：红条即禁发（title 带原因）、其他提示让位单条出口、
      重新生成出正常音频自动解除
  V24（#121 三进宫，2026-09-02 KKXSTU）服务端有声终审优先：voice_meta.speech=
      'voiced' 时即便本地解码出静音也**绝不**亮红条（红条元素 data-detector=
      v3-server / data-basis=server-voiced，实际使用行出「✓ 已核有声」）；
      speech='silent' 时正常音频也直接红条+禁发（服务端确认无声）
  V25（#149）过期优先于无声：无声红条亮着时换音色 → 红条让位、过期说明可见
      （KKXSTU 截图「下拉美月／实际使用张景光」被读成选X出Y的成因）；换回原音色
      红条回来；服务端选声失配（reason=voice_selection:*）→ 错误态而非预览
  V26（#250 N-4 B，钧机 1258/1315 两张截图）结果区**唯一结论行**：服务端 voiced 时
      整个结果区只有一条 banner（verdict:ok），任何可见元素都不含「疑似无声」，实际
      使用行不再另下结论；`hidden` 属性必须真能藏住 banner（计算样式 display:none）——
      0831 起 .pv-note{display:flex} 架空 hidden、红条橙条恒常可见的病根就钉在这里；
      阻发结论行内自带「重新生成」
  V27（#250 N-4 C 前置）过期时发送钮整个让位、主按钮是「重新生成」；解除后回位
  V28（#250 N-4 C）过期只由真实变更触发：中文客户 + 中文文本 identity 不译（服务端
      voice_translated=false）→ 生成完成不过期；关跟随翻译才过期、拨回原状解除；会话
      语言异步回填同值 / refreshXl 不过期；旧后端缺 target_lang_resolved 同样不过期；
      双侧未知不过期，之后解析出**不同**语种（发送会真的改译）才过期
  V29（#250 N-4 D）配置非法＝第三种结论「⚠ 配置需修正」：effective-config 带
      voice_config_blocked → 生成前状态行红点 + 详情自动展开（服务端人话 + 去语音页深链
      #profile=<id>&tab=voice）；tts-test reason=voice_config:* → 黄字 + 同一深链、
      无「重试」、无音频、无任何「疑似无声」banner

用法::

    python tools/verify_cp_voice_ui.py             # 门禁模式
    python tools/verify_cp_voice_ui.py --headed    # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]

TEXT_A = "我还在忙饭，你吃饭了吗？"
TEXT_B = "刚忙完，正想着你呢。"

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>cp-voice state probe</title></head>
<body style="margin:16px;font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;max-width:360px;">
<script>
window.CP_LANG = 'zh';   // 词典语言钉死，断言与坐席实际看到的中文一致
</script>
<script src="@@I18N_JS@@"></script>
<script>
window.__calls = { tts: [], send: [], profiles: 0 };
window.__ttsDelayMs = 30;
window.__ttsFail = false;
window.__ttsFallback = false;
window.__ttsFbReason = '';   // P0 2026-08-31 回落原因分支（lang_unsupported/quota/…）
window.__ttsFbLang = '';
window.__ttsSilent = false;  // true=回真实静音 WAV（sniff 阻发闸场景）
window.__ttsSpeech = '';     // V24：服务端有声终审 voice_meta.speech（''=旧后端缺键）
window.__ttsSelReject = '';  // V25：服务端选声失配 reason（voice_selection:<why>）
window.__ttsEchoRequested = false;   // V25：新后端回填 voice_meta.requested_persona_id
window.__sendDelayMs = 30;
window.__sendFail = false;
// 0.1s 静音 PCM16 WAV（可被 decodeAudioData 解码；峰值/RMS 双零 → 必判无声）
window.__silentWavUrl = (() => {
  const sr = 16000, n = 1600;
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const w = (o, s2) => { for (let i = 0; i < s2.length; i++) v.setUint8(o + i, s2.charCodeAt(i)); };
  w(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w(8, 'WAVEfmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, n * 2, true);
  let bin = ''; const u8 = new Uint8Array(buf);
  for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
  return 'data:audio/wav;base64,' + btoa(bin);
})();
window.__stubClient = {
  async voiceProfiles() {
    window.__calls.profiles++;
    return { ok: true, default: {},
      profiles: [{ persona_id: 'linda', name: 'Linda', is_clone: true, ready: true }] };
  },
  async listPersonas() { return { summary: [] }; },
  async voiceTts(args) {
    window.__calls.tts.push(JSON.parse(JSON.stringify(args || {})));
    await new Promise((r) => setTimeout(r, window.__ttsDelayMs));
    if (window.__ttsFail) return { ok: false, error: 'boom-503' };
    // V25：服务端选声失配拒绝（#149 契约：reason=voice_selection:<why>）
    if (window.__ttsSelReject) {
      return { ok: false, reason: 'voice_selection:' + window.__ttsSelReject,
        requested_persona_id: (args && args.persona_id) || '',
        resolved_persona_id: 'zhang_jingguang', error: 'mismatch' };
    }
    // P0-V2b 译声契约模拟：带 target_lang 即回译稿三件套（与服务端响应同形）。
    // #121：显式目标必须回显所请求的语种（真服务端如此），'auto' 才由「服务端」
    // 解析成会话语言（夹具恒 ja）——旧夹具恒回 ja 与真实契约不符。
    const xl = !!(args && args.target_lang);
    const req = xl ? String(args.target_lang) : '';
    const vm = { persona_id: args && args.persona_id || '', provider: 'edge_tts',
        voice: 'zh-CN-XiaoxiaoNeural', emotion: 'warm',
        fallback_from: window.__ttsFallback ? 'avatar_clone' : '',
        fallback_reason: window.__ttsFallback ? (window.__ttsFbReason || '') : '',
        fallback_lang: window.__ttsFallback ? (window.__ttsFbLang || '') : '' };
    // V24：新后端有声终审（additive；''=旧后端缺键，前端走本地兜底）
    if (window.__ttsSpeech) { vm.speech = window.__ttsSpeech; vm.speech_basis = 'fixture'; }
    if (window.__ttsEchoRequested) vm.requested_persona_id = (args && args.persona_id) || '';
    return { ok: true,
      audio_url: window.__ttsSilent ? window.__silentWavUrl : 'data:audio/mp3;base64,AAAA',
      filename: 'ttspreview-00aabbccdd.mp3',
      duration_sec: window.__ttsSilent ? 0.1 : 0,
      voice_translated: xl,
      spoken_text: xl ? 'こんにちは、теスト譯稿です' : '',
      target_lang: xl ? (req === 'auto' ? 'ja' : req) : '',
      voice_meta: vm };
  },
  async sendVoice(body) {
    window.__calls.send.push(JSON.parse(JSON.stringify(body || {})));
    await new Promise((r) => setTimeout(r, window.__sendDelayMs));
    if (window.__sendFail) return { ok: false, message: 'peer-offline' };
    // 所听即所发：带 preview_filename 即按复用命中回显（与服务端契约同语义）
    return { ok: true, reused_preview: !!(body && body.preview_filename),
      voice_meta: { provider: 'edge_tts', emotion: 'warm',
        translated: !!(body && body.target_lang),
        target_lang: (body && body.target_lang) ? 'ja' : '' } };
  },
};
window.__xlEvents = 0;
document.addEventListener('cp-voice-xl-changed', () => { window.__xlEvents++; });
window.__sentEvents = 0;
window.__sentDetails = [];
document.addEventListener('cp-voice-sent', (ev) => {
  window.__sentEvents++;
  window.__sentDetails.push((ev && ev.detail) || {});
});
</script>
<script src="@@VOICE_JS@@"></script>
<script>
const el = document.createElement('cp-voice');
el.client = window.__stubClient;
document.body.appendChild(el);
el._hintResetMs = 300;   // 「已发送」驻留缩短，验收不干等 4s（组件暴露的可调字段）
el.context = { platform: 'whatsapp', accountId: 'acc1', chatKey: 'peer1',
               conversationId: 'whatsapp:acc1:peer1' };
window.__el = el;
window.q = (sel) => window.__el.shadowRoot.querySelector(sel);
window.qa = (sel) => Array.from(window.__el.shadowRoot.querySelectorAll(sel));
window.setText = (t) => {
  const ta = window.q('[data-role="text"]');
  ta.value = t;
  ta.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
};
// #250（N-4 B）：结果区唯一结论行 [data-role="verdict"]，四态 stale/blocked/ok/unverified。
// 一律读**计算样式**判可见（el.hidden 属性曾被 .pv-note{display:flex} 架空 → 三条同屏）。
window.vd = () => {
  const n = window.q('[data-role="verdict"]');
  if (!n) return { has: false, visible: false, state: '', kind: '', speech: '', basis: '', detector: '', text: '', display: '' };
  const cs = getComputedStyle(n);
  return { has: true, visible: !n.hidden && cs.display !== 'none' && n.offsetHeight > 0,
           state: n.getAttribute('data-state') || '', kind: n.getAttribute('data-kind') || '',
           speech: n.getAttribute('data-speech') || '', basis: n.getAttribute('data-basis') || '',
           detector: n.getAttribute('data-detector') || '', text: n.textContent || '',
           display: cs.display };
};
window.visibleBanners = () => window.qa('.preview .pv-verdict, .preview .pv-note')
  .filter((n) => !n.hidden && getComputedStyle(n).display !== 'none' && n.offsetHeight > 0)
  .map((n) => (n.getAttribute('data-role') || '') + ':' + (n.getAttribute('data-state') || n.getAttribute('data-reason') || ''));
window.snap = () => {
  const box = window.q('[data-role="preview"]');
  const main = window.q('[data-role="gen-main"]');
  const send = window.q('[data-act="send"]');
  const v = window.vd();
  const note = (v.has && v.visible && v.state === 'stale') ? window.q('[data-role="verdict"]') : null;
  // P2 2026-08-30：状态/结果提示改写进专用 msg 行（旧 .hint 首匹配会命中可能
  // hidden 的音色状态条 effstatus——提示写进看不见的元素）
  const hint = window.q('[data-role="msg"]');
  return {
    xlOn: !!(window.q('[data-role="xlfollow"]') || {}).checked,
    xlHint: (window.q('[data-role="xlhint"]') || {}).textContent || '',
    spokenVisible: !!window.q('.preview .pv-spoken'),
    boxHidden: !box || box.hidden, boxHtmlLen: box ? box.innerHTML.length : 0,
    boxStale: !!(box && box.classList.contains('stale')),
    mainLabel: main ? main.textContent : '', mainDisabled: !!(main && main.disabled),
    sendLabel: send ? send.textContent : null, sendDisabled: send ? send.disabled : null,
    sendHidden: send ? (send.hidden || getComputedStyle(send).display === 'none') : null,
    regenMainVisible: (() => { const r = window.q('[data-role="regen-main"]'); return !!(r && !r.hidden && getComputedStyle(r).display !== 'none'); })(),
    noteVisible: !!note,
    verdictState: v.state, verdictVisible: v.visible,
    hasAudio: !!window.q('.preview audio'),
    hasRegen: !!(box && !box.hidden && window.qa('.preview [data-act="tts"]').length),
    hasClear: !!window.q('.preview [data-act="clear-preview"]'),
    hasCancel: !!(box && !box.hidden && window.q('.preview [data-act="cancel-gen"]')),
    warnVisible: !!window.q('.preview .warn'),
    cntText: (window.q('[data-role="cnt"]') || {}).textContent || '',
    cntErr: !!(window.q('[data-role="cnt"]') && window.q('[data-role="cnt"]').classList.contains('err')),
    cntHidden: !window.q('[data-role="cnt"]') || window.q('[data-role="cnt"]').hidden,
    hintText: hint ? hint.textContent : '', hintClass: hint ? hint.className : '',
    taValue: (window.q('[data-role="text"]') || {}).value || '',
    tts: window.__calls.tts.length, send: window.__calls.send.length,
    sentEvents: window.__sentEvents,
  };
};
window.waitIdle = async (ms) => {
  const t0 = Date.now();
  while (Date.now() - t0 < (ms || 3000)) {
    if (!window.__el._busy) return true;
    await new Promise((r) => setTimeout(r, 20));
  }
  return !window.__el._busy;
};
window.waitSilent = async (ms) => {   // sniff 是异步 fetch+decode，等结论行转 blocked
  const t0 = Date.now();
  while (Date.now() - t0 < (ms || 3000)) {
    const v = window.vd();
    if (v.visible && v.state === 'blocked') return true;
    await new Promise((r) => setTimeout(r, 30));
  }
  return false;
};
</script>
</body></html>
"""


def build_fixture_page(tmp: Path) -> Path:
    html = (_HTML
            .replace("@@I18N_JS@@", (ENGINE / "shared/copilot/i18n/cp-i18n.js").as_uri())
            .replace("@@VOICE_JS@@", (ENGINE / "shared/copilot/components/cp-voice.js").as_uri()))
    fp = tmp / "probe_voice.html"
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
        print(f"\n== cp-voice 状态机验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def run(page, ck: Checker, dlg) -> None:
    ev = page.evaluate

    # V1 初始渲染
    s = ev("snap()")
    ck.check("V1 生成按钮动词化「生成语音」", "生成语音" in s["mainLabel"], s["mainLabel"])
    ck.check("V1 预览初始隐藏", s["boxHidden"])

    # V2 空文本
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    s = ev("snap()")
    ck.check("V2 空文本提示且不发请求", "请先输入文字" in s["hintText"]
             and "err" in s["hintClass"] and s["tts"] == 0)

    # V3 正常生成（busy 态 → 预览成形 → 请求带会话上下文）
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    s = ev("snap()")
    ck.check("V3 请求期按钮禁用+生成中", s["mainDisabled"] and "生成中" in s["mainLabel"])
    ev("waitIdle()")
    s = ev("snap()")
    ck.check("V3 预览成形（音频+重新生成+清除+发送）",
             (not s["boxHidden"]) and s["hasAudio"] and s["hasRegen"] and s["hasClear"]
             and s["sendLabel"] is not None and s["tts"] == 1)
    args = ev("window.__calls.tts[0]")
    ck.check("V3 试听带会话上下文（试听=发送契约）",
             args.get("chat_key") == "peer1" and args.get("platform") == "whatsapp"
             and args.get("account_id") == "acc1", str(args))

    # V4 生成在途连点只发一次
    ev("() => { window.__ttsDelayMs = 300; }")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    ck.check("V4 生成在途连点只发一次", s["tts"] == 2, f"tts={s['tts']}")
    ev("() => { window.__ttsDelayMs = 30; }")

    # V5 ✕ 清除（报障①）
    ev("() => { q('.preview [data-act=\"clear-preview\"]').click(); }")
    s = ev("snap()")
    ck.check("V5 清除后预览隐藏且清空", s["boxHidden"] and s["boxHtmlLen"] == 0)
    ck.check("V5 清除不动已输入文字", s["taValue"] == TEXT_A, s["taValue"])

    # V6 重新生成入口常驻（报障②）
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    ck.check("V6 预览区重新生成可用", s["tts"] == 4 and s["hasAudio"], f"tts={s['tts']}")

    # V7 改文字 → 过期；改回 → 解除
    ev(f"() => setText({TEXT_B!r})")
    s = ev("snap()")
    ck.check("V7 改文字后预览过期（边框+警示+发送禁用）",
             s["boxStale"] and s["noteVisible"] and s["sendDisabled"] is True)
    ev(f"() => setText({TEXT_A!r})")
    s = ev("snap()")
    ck.check("V7 改回原文自动解除过期", (not s["boxStale"]) and (not s["noteVisible"])
             and s["sendDisabled"] is False)

    # V8 换音色 → 过期；换回 → 解除
    changed = ev("""() => {
      const sel = q('[data-role="persona"]');
      if (!sel || !Array.from(sel.options).some((o) => o.value === 'linda')) return false;
      sel.value = 'linda';
      sel.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
      return true;
    }""")
    s = ev("snap()")
    ck.check("V8 换音色后预览过期", changed and s["boxStale"] and s["sendDisabled"] is True)
    ev("""() => {
      const sel = q('[data-role="persona"]');
      sel.value = '';
      sel.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    s = ev("snap()")
    ck.check("V8 换回原音色解除过期", not s["boxStale"])

    # V9 发送成功：复位 + 幂等键 + 事件 + 提示自动恢复
    ev("() => { q('.preview [data-act=\"send\"]').click(); }")
    s = ev("snap()")
    ck.check("V9 发送请求期按钮禁用+发送中", s["sendDisabled"] is True and "发送中" in (s["sendLabel"] or ""))
    ev("waitIdle()")
    s = ev("snap()")
    ck.check("V9 发送成功全复位（预览+文字）", s["boxHidden"] and s["taValue"] == ""
             and s["send"] == 1 and s["sentEvents"] == 1)
    ck.check("V9 发送成功提示", "已发送" in s["hintText"] and "ok" in s["hintClass"], s["hintText"])
    body = ev("window.__calls.send[0]")
    ck.check("V9 幂等键 client_msg_id=cpv-*",
             str(body.get("client_msg_id") or "").startswith("cpv-"), str(body.get("client_msg_id")))
    ck.check("V9 所听即所发：发送带回试听产物名",
             body.get("preview_filename") == "ttspreview-00aabbccdd.mp3",
             str(body.get("preview_filename")))
    det = ev("window.__sentDetails[0]")
    ck.check("V9 cp-voice-sent 事件带 reused 分桶", det and det.get("reused") is True, str(det))
    page.wait_for_timeout(500)   # _hintResetMs=300 + 余量
    s = ev("snap()")
    ck.check("V9 提示驻留后清空（常驻引导语在静态行不重复）",
             s["hintText"] == "", s["hintText"])

    # V10 发送在途连点只发一次
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    ev("() => { window.__sendDelayMs = 300; }")
    ev("() => { const b = q('.preview [data-act=\"send\"]'); b.click(); b.click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    ck.check("V10 发送在途连点只发一次", s["send"] == 2, f"send={s['send']}")
    ev("() => { window.__sendDelayMs = 30; }")

    # V11 发送失败：预览保留、按钮恢复、透传服务端 message
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    ev("() => { window.__sendFail = true; }")
    ev("() => { q('.preview [data-act=\"send\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    ck.check("V11 发送失败预览保留+按钮恢复",
             (not s["boxHidden"]) and s["hasAudio"] and s["sendDisabled"] is False)
    ck.check("V11 失败文案透传服务端 message", "peer-offline" in s["hintText"], s["hintText"])
    ev("() => { window.__sendFail = false; }")

    # V12 生成失败：错误态自带 重试/✕
    ev("() => { window.__ttsFail = true; }")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    err = ev("() => { const n = q('.preview .err'); return n ? n.textContent : ''; }")
    ck.check("V12 生成失败错误态带重试+清除", (not s["boxHidden"]) and s["hasRegen"] and s["hasClear"]
             and "boom-503" in err, err)
    ev("() => { window.__ttsFail = false; }")

    # V13 回落警示
    ev("() => { window.__ttsFallback = true; }")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    ck.check("V13 克隆回落黄字警示", s["warnVisible"])
    ev("() => { window.__ttsFallback = false; }")

    # V14 cp-fill 程序化填入 → 过期标记
    ev("""() => {
      window.__el.dispatchEvent(new CustomEvent('cp-fill',
        { detail: { text: '换一句草稿文案' } }));
    }""")
    s = ev("snap()")
    ck.check("V14 cp-fill 填入触发过期", s["boxStale"] and s["taValue"] == "换一句草稿文案")

    # V15 生成在途切会话 → 结果作废（epoch 防串会话）
    ev("() => { q('.preview [data-act=\"clear-preview\"]').click(); }")
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { window.__ttsDelayMs = 400; }")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("""() => {
      window.__el.context = { platform: 'whatsapp', accountId: 'acc1', chatKey: 'peer2',
                              conversationId: 'whatsapp:acc1:peer2' };
    }""")
    page.wait_for_timeout(700)
    s = ev("snap()")
    ck.check("V15 在途结果不回写新会话（预览空+按钮复位）",
             s["boxHidden"] and (not s["mainDisabled"]) and "生成语音" in s["mainLabel"])

    # V16 生成可取消（epoch 代际作废：在途结果静默丢弃，界面立即复位）
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { window.__ttsDelayMs = 400; }")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    s = ev("snap()")
    ck.check("V16 生成期出现取消按钮", s["hasCancel"] and s["mainDisabled"])
    ev("() => { q('.preview [data-act=\"cancel-gen\"]').click(); }")
    s = ev("snap()")
    ck.check("V16 取消立即复位（预览隐藏+按钮可用）",
             s["boxHidden"] and (not s["mainDisabled"]) and "生成语音" in s["mainLabel"])
    page.wait_for_timeout(600)
    s = ev("snap()")
    ck.check("V16 取消后在途结果被丢弃（不回写）", s["boxHidden"])
    ev("() => { window.__ttsDelayMs = 30; }")

    # V17 字数计（与服务端试听上限同口径，超限前端先拦）
    ev("() => setText('长'.repeat(401))")
    s = ev("snap()")
    ck.check("V17 超限红字计数+生成禁用",
             s["cntText"] == "401/400" and s["cntErr"] and s["mainDisabled"])
    ev(f"() => setText({TEXT_A!r})")
    s = ev("snap()")
    ck.check("V17 回到限内自动恢复",
             (not s["cntErr"]) and (not s["mainDisabled"]) and s["cntText"].endswith("/400"))
    ev("() => setText('')")
    s = ev("snap()")
    ck.check("V17 空文本计数隐藏", s["cntHidden"])

    # V18 译声（P0-V2b 2026-08-30）：默认跟随翻译（target_lang='auto'）、译稿行
    # 可见、开关翻转=预览过期+请求不再带目标语、发送事件带 translated 分桶
    s = ev("snap()")
    ck.check("V18 跟随翻译默认开", s["xlOn"] is True)
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    args = ev("window.__calls.tts[window.__calls.tts.length-1]")
    ck.check("V18 试听请求带 target_lang='auto'", args.get("target_lang") == "auto", str(args))
    ck.check("V18 译稿行可见（所见即所念）", s["spokenVisible"])
    ev("() => { q('.preview [data-act=\"send\"]').click(); }")
    ev("waitIdle()")
    det = ev("window.__sentDetails[window.__sentDetails.length-1]")
    body = ev("window.__calls.send[window.__calls.send.length-1]")
    ck.check("V18 发送请求带 target_lang", body.get("target_lang") == "auto", str(body.get("target_lang")))
    ck.check("V18 cp-voice-sent 带 translated 分桶", det and det.get("translated") is True, str(det))
    s = ev("snap()")
    ck.check("V18 发送成功提示带「已译成」", "已译成" in s["hintText"], s["hintText"])
    # 开关翻转：已生成的试听立即过期（防「听中文原文、发出去外语」的所听非所发）
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    ev("""() => {
      const cb = q('[data-role="xlfollow"]');
      cb.checked = false;
      cb.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    s = ev("snap()")
    ck.check("V18 关跟随后预览过期+发送禁用",
             s["boxStale"] and s["sendDisabled"] is True and s["noteVisible"])
    ck.check("V18 关跟随后提示「按原文发声」", "按原文发声" in s["xlHint"], s["xlHint"])
    xlev = ev("window.__xlEvents")
    ck.check("V18 开关翻转广播 cp-voice-xl-changed", xlev >= 1, f"events={xlev}")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    args = ev("window.__calls.tts[window.__calls.tts.length-1]")
    ck.check("V18 关跟随后请求不带 target_lang", "target_lang" not in args, str(args))
    ck.check("V18 关跟随后无译稿行", not s["spokenVisible"])
    ev("""() => {
      const cb = q('[data-role="xlfollow"]');
      cb.checked = true;
      cb.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    s = ev("snap()")
    ck.check("V18 重新开跟随=语言维度再次过期", s["boxStale"] and s["sendDisabled"] is True)

    # V19 宿主目标语偏好（P0-V2c 2026-08-30 用户实测修正）：坐席显式选了
    # 「我的消息 → en」→ 请求带 target_lang='en'（auto 不得吃掉显式意图）、
    # 预告行立即显示「将译成 英语」（不依赖后端 conv_lang）；偏好换语言=过期
    ev("() => { q('.preview [data-act=\"clear-preview\"]').click(); }")
    ev("() => { window.__cpVoiceXlPref = () => 'en'; window.__el.refreshXl(); }")
    s = ev("snap()")
    ck.check("V19 显式偏好预告「将译成 英语」", "英语" in s["xlHint"], s["xlHint"])
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    args = ev("window.__calls.tts[window.__calls.tts.length-1]")
    ck.check("V19 试听请求带显式 target_lang='en'",
             args.get("target_lang") == "en", str(args))
    ev("() => { window.__cpVoiceXlPref = () => 'ja'; window.__el.refreshXl(); }")
    s = ev("snap()")
    ck.check("V19 偏好换语言=预览过期+发送禁用",
             s["boxStale"] and s["sendDisabled"] is True)
    ev("() => { window.__cpVoiceXlPref = null; window.__el.refreshXl(); }")

    # V20（#121 skuio 0831 原图 924）：宿主「我的消息 →」偏好**异步加载**出的值
    # 与服务端试听时真实使用的目标语一致（auto 已解析成 ja）→ 绝不误报过期
    # ——坐席什么都没改，'auto'→'ja' 只是表示层变化；换成真不同的语种才过期。
    ev("() => { q('.preview [data-act=\"clear-preview\"]').click(); }")
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    args = ev("window.__calls.tts[window.__calls.tts.length-1]")
    ck.check("V20 无偏好时试听按 auto 请求", args.get("target_lang") == "auto", str(args))
    ev("() => { window.__cpVoiceXlPref = () => 'ja'; window.__el.refreshXl(); }")
    s = ev("snap()")
    ck.check("V20 偏好迟到但与服务端已用目标一致=不误报过期",
             (not s["boxStale"]) and s["sendDisabled"] is False,
             f"stale={s['boxStale']} sendDisabled={s['sendDisabled']}")
    ev("() => { window.__cpVoiceXlPref = () => 'en'; window.__el.refreshXl(); }")
    s = ev("snap()")
    ck.check("V20 偏好换成真不同语种=照常过期",
             s["boxStale"] and s["sendDisabled"] is True)
    ev("() => { window.__cpVoiceXlPref = null; window.__el.refreshXl(); }")

    # V21（P0 2026-08-31 提示风暴复盘）：回落原因分支——语种改道是刻意保护，
    # 走 info 蓝条（非警告色）+「改发原文」一键出路（关跟随翻译并自动重生成）；
    # 顶部语种预告与该说明同因去重，同一件事只出现一处。
    ev("() => { q('.preview [data-act=\"clear-preview\"]').click(); }")
    ev("() => { window.__ttsFallback = true; window.__ttsFbReason = 'lang_unsupported';"
       " window.__ttsFbLang = 'ja'; }")
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    fb = ev("""() => {
      const n = q('[data-role="fallback-note"]');
      return { has: !!n, hidden: n ? n.hidden : true,
               reason: n ? (n.getAttribute('data-reason') || '') : '',
               info: !!(n && n.classList.contains('info')),
               btn: !!(n && n.querySelector('[data-act="xl-off-regen"]')) };
    }""")
    ck.check("V21 语种改道走 info 蓝条（data-reason=lang）",
             fb["has"] and (not fb["hidden"]) and fb["info"] and fb["reason"] == "lang",
             str(fb))
    ck.check("V21 说明自带「改发原文」一键出路", fb["btn"])
    s = ev("snap()")
    ck.check("V21 顶部语种预告让位（同因去重）", s["xlHint"] == "", s["xlHint"])
    ck.check("V21 语种改道不禁发（确认后可发）", s["sendDisabled"] is False)
    n_tts = ev("window.__calls.tts.length")
    ev("() => { q('.preview [data-act=\"xl-off-regen\"]').click(); }")
    ev("waitIdle()")
    args = ev("window.__calls.tts[window.__calls.tts.length-1]")
    s = ev("snap()")
    ck.check("V21 一键出路=关跟随+按原文重生成",
             ev("window.__calls.tts.length") == n_tts + 1
             and "target_lang" not in args and s["xlOn"] is False, str(args))
    ev("() => { window.__ttsFbReason = ''; window.__ttsFbLang = ''; }")

    # V22（P1-3）：降级发送显式确认——取消=不发送；确认=发送；同会话记住选择
    # 不重复骚扰（黄字警示挡不住肌肉记忆，确认弹层是「回落必须显式」的强化版）
    ev(f"() => setText({TEXT_B!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    dlg["accept"] = False
    dlg["count"] = 0
    n_send = ev("window.__calls.send.length")
    ev("() => { q('.preview [data-act=\"send\"]').click(); }")
    ev("waitIdle()")
    ck.check("V22 降级发送先确认（取消=不发送）",
             dlg["count"] == 1 and ev("window.__calls.send.length") == n_send,
             f"dialogs={dlg['count']} msg={dlg['last'][:40]}")
    dlg["accept"] = True
    ev("() => { q('.preview [data-act=\"send\"]').click(); }")
    ev("waitIdle()")
    ck.check("V22 确认后发送成功",
             dlg["count"] == 2 and ev("window.__calls.send.length") == n_send + 1)
    ev(f"() => setText({TEXT_B!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    ev("() => { q('.preview [data-act=\"send\"]').click(); }")
    ev("waitIdle()")
    ck.check("V22 同会话记住选择不再弹",
             dlg["count"] == 2 and ev("window.__calls.send.length") == n_send + 2)

    # V23（P0 2026-08-31）：疑似无声=真闸门——红条出现即禁发、回落说明让位
    # （单条出口）；重新生成出正常音频自动解除。红字劝「不要发送」而按钮亮蓝
    # 可点（0831 截图实录）就是本场景防的回归。
    ev("() => { window.__ttsSilent = true; }")
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    ok_silent = ev("waitSilent(4000)")
    s = ev("snap()")
    ck.check("V23 无声产物红条+禁发", bool(ok_silent) and s["sendDisabled"] is True,
             f"silent={ok_silent} sendDisabled={s['sendDisabled']}")
    painted = ev("() => { const c = q('[data-role=\"wave\"]');"
                 " return !!(c && c.getAttribute('data-painted') === '1'); }")
    ck.check("V23 响度包络已绘制（可解码产物成像，无声=扁平基线）", painted)
    fb_hidden = ev("() => { const n = q('[data-role=\"fallback-note\"]');"
                   " return !n || n.hidden; }")
    ck.check("V23 无声时回落说明让位（单条出口）", fb_hidden)
    ev("() => { window.__ttsSilent = false; }")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    page.wait_for_timeout(400)   # 给 sniff 一拍：正常音频不得亮红条
    silent_now = ev("() => vd().visible && vd().state === 'blocked'")
    s = ev("snap()")
    ck.check("V23 重新生成解除阻发", (not silent_now) and s["sendDisabled"] is False,
             f"silent={silent_now} sendDisabled={s['sendDisabled']}")
    ev("() => { window.__ttsFallback = false; }")

    # V24（#121 三进宫，2026-09-02 KKXSTU）：服务端有声终审优先——同一份本地解码为
    # 静音的产物，服务端说 voiced（Whisper 全文转录命中）→ 绝不亮红条、发送可点、
    # 实际使用行带「✓ 已核有声」；服务端说 silent → 正常音频也红条+禁发。
    ev("() => { window.__ttsSilent = true; window.__ttsSpeech = 'voiced'; }")
    ev(f"() => setText({TEXT_B!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    page.wait_for_timeout(700)   # 给本地解码一拍：它必须**不**能翻案
    v24 = ev("""() => {
      const v = vd();
      const meta = q('.preview .hint[title]');
      return Object.assign(v, { metaText: meta ? meta.textContent : '', banners: visibleBanners() });
    }""")
    s = ev("snap()")
    ck.check("V24 服务端 voiced 压过本地静音解码：结论=可发送、无阻发",
             v24["visible"] and v24["state"] == "ok" and s["sendDisabled"] is False, str(v24))
    ck.check("V24 结论行带 v4-single 标记与 server-voiced 依据（F12 可查证）",
             v24["detector"] == "v4-single" and v24["basis"] == "server-voiced", str(v24))
    ck.check("V24 结论行出「已核有声，可发送」且实际使用行不再另下结论",
             "已核有声" in v24["text"] and "已核有声" not in v24["metaText"],
             f"verdict={v24['text']!r} meta={v24['metaText']!r}")
    ck.check("V26 #250 钧机形态：整个结果区只有一条 banner（无「疑似无声」并列）",
             v24["banners"] == ["verdict:ok"] and "疑似无声" not in ev(
                 "() => qa('.preview *').filter(n => n.offsetHeight > 0).map(n => n.textContent).join('|')"),
             str(v24["banners"]))
    hidden_css = ev("""() => {
      const n = q('[data-role="verdict"]');
      n.hidden = true; const d = getComputedStyle(n).display; const h = n.offsetHeight;
      n.hidden = false;
      return { display: d, height: h };
    }""")
    ck.check("V26 hidden 属性真能藏住 banner（计算样式 display:none，0831 起的 CSS 病根）",
             hidden_css["display"] == "none" and hidden_css["height"] == 0, str(hidden_css))
    painted = ev("() => { const c = q('[data-role=\"wave\"]');"
                 " return !!(c && c.getAttribute('data-painted') === '1'); }")
    ck.check("V24 响度包络仍绘制（本地解码只成像不裁决）", painted)
    ev("() => { window.__ttsSilent = false; window.__ttsSpeech = 'silent'; }")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    v24b = ev("() => vd()")
    ck.check("V24 服务端 silent：正常音频也直接阻发（不等本地解码）",
             v24b["visible"] and v24b["state"] == "blocked" and s["sendDisabled"] is True
             and v24b["speech"] == "silent" and v24b["kind"] == "silent", str(v24b))
    ck.check("V24 服务端确认文案（区别于本地疑似）", "服务端已确认" in v24b["text"], v24b["text"])
    ck.check("V26 阻发结论行内带「重新生成」出路",
             ev("() => !!q('[data-role=\"verdict\"] [data-act=\"tts\"]')"))
    ev("() => { window.__ttsSpeech = ''; }")

    # V25（#149）：过期优先于无声——红条亮着时换音色，红条让位、过期说明可见（不再
    # 出现「下拉=美月 / 实际使用=张景光 / 只有红条」的选X出Y误读）；换回原音色红条回来。
    ev("() => { window.__ttsSilent = true; }")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    ok_silent = ev("waitSilent(4000)")
    ck.check("V25 前置：本地兜底红条已亮", bool(ok_silent))
    ev("""() => {
      const sel = q('[data-role="persona"]');
      sel.value = 'linda';
      sel.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    s = ev("snap()")
    silent_vis = ev("() => vd().visible && vd().state === 'blocked'")
    ck.check("V25 换音色后：过期说明可见、红条让位、发送仍禁用",
             s["noteVisible"] and (not silent_vis) and s["sendDisabled"] is True,
             f"stale={s['noteVisible']} silent={silent_vis} sendDisabled={s['sendDisabled']}")
    ck.check("V27 #250 过期时发送钮让位、主按钮是「重新生成」（不是灰掉的发送）",
             s["sendHidden"] is True and s["regenMainVisible"]
             and ev("() => visibleBanners()") == ["verdict:stale"],
             f"sendHidden={s['sendHidden']} regen={s['regenMainVisible']}")
    ev("""() => {
      const sel = q('[data-role="persona"]');
      sel.value = '';
      sel.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    s = ev("snap()")
    silent_vis = ev("() => vd().visible && vd().state === 'blocked'")
    ck.check("V25 换回原音色：过期解除、红条回来、仍禁发",
             (not s["noteVisible"]) and silent_vis and s["sendDisabled"] is True,
             f"stale={s['noteVisible']} silent={silent_vis}")
    ck.check("V27 解除过期后发送钮回位、「重新生成」主按钮收起",
             s["sendHidden"] is False and (not s["regenMainVisible"]))
    ev("() => { window.__ttsSilent = false; }")
    # 服务端选声失配 → 错误态（带重试/清除），不摆出别人的声音当预览
    ev("""() => {
      const sel = q('[data-role="persona"]');
      sel.value = 'linda';
      sel.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
      window.__ttsSelReject = 'source_mismatch';
    }""")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    err = ev("() => { const n = q('.preview .err'); return n ? n.textContent : ''; }")
    ck.check("V25 服务端选声失配 → 错误态（无音频、带重试/清除）",
             (not s["hasAudio"]) and s["hasRegen"] and s["hasClear"]
             and "zhang_jingguang" in err, err)
    ev("() => { window.__ttsSelReject = ''; window.__ttsEchoRequested = true; }")
    ev("() => { q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    args = ev("window.__calls.tts[window.__calls.tts.length-1]")
    ck.check("V25 新后端回填 requested==所选 → 正常预览",
             s["hasAudio"] and args.get("persona_id") == "linda", str(args))
    ev("() => { window.__ttsEchoRequested = false; }")
    ev("""() => {
      const sel = q('[data-role="persona"]');
      sel.value = '';
      sel.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")

    # V28（#250 N-4 C，钧机 1258/1315）：中文客户 + 中文文本 → 服务端 'auto' 解析成 zh、
    # identity 不译（voice_translated=false / target_lang=''）。旧逻辑把基准记成 'auto'
    # 与会话语言 'zh' 一比即「过期」，生成一结束发送就灰。修后：只有目标语真变才过期。
    ev("() => { q('.preview [data-act=\"clear-preview\"]').click(); }")
    ev("""() => {
      window.__origVoiceTts = window.__stubClient.voiceTts;
      window.__xlResolved = 'zh';          // 服务端 target_lang_resolved；undefined=旧后端缺键
      window.__stubClient.voiceTts = async (args) => {
        const d = await window.__origVoiceTts(args);
        d.voice_translated = false; d.spoken_text = ''; d.target_lang = '';
        if (window.__xlResolved !== undefined) d.target_lang_resolved = window.__xlResolved;
        d.voice_meta.speech = 'voiced'; d.voice_meta.speech_basis = 'transcript+energy';
        return d;
      };
      window.__el._effConvLang = 'zh';     // effective-config 已回 conv_lang=zh
      // 钧机默认档：「跟随翻译发声」开（V21 的一键出路曾把它关掉，这里拨回开）
      const cb = q('[data-role="xlfollow"]');
      cb.checked = true;
      cb.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    ev("() => setText('尽量提前确定一下 乔金16号需要离境一趟')")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    args = ev("window.__calls.tts[window.__calls.tts.length-1]")
    ck.check("V28 钧机形态：跟随翻译开（请求带 auto）、同语种不译 → 生成完成不过期、结论 ok、发送可点",
             args.get("target_lang") == "auto" and (not s["boxStale"]) and s["verdictState"] == "ok"
             and s["sendDisabled"] is False and s["sendHidden"] is False,
             f"xl={args.get('target_lang')} stale={s['boxStale']} verdict={s['verdictState']} sendDisabled={s['sendDisabled']}")
    ev("""() => {
      const cb = q('[data-role="xlfollow"]');
      cb.checked = false;
      cb.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    s = ev("snap()")
    ck.check("V28 真的关掉跟随翻译 → 过期（真实变更仍触发）", s["boxStale"] and s["verdictState"] == "stale")
    ev("""() => {
      const cb = q('[data-role="xlfollow"]');
      cb.checked = true;
      cb.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    }""")
    s = ev("snap()")
    ck.check("V28 开关拨回原状 → 过期解除（设置没有净变化）", not s["boxStale"])
    ev("() => { window.__el._effConvLang = 'zh'; window.__el.refreshXl(); }")
    s = ev("snap()")
    ck.check("V28 会话语言异步回填同值 / 面板刷新 → 不过期", not s["boxStale"])
    # 旧后端（无 target_lang_resolved）：客户端已知会话语言即基准，同样不过期
    ev("() => { window.__xlResolved = undefined; q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    ck.check("V28 旧后端缺 target_lang_resolved：以客户端会话语言为基准，不过期",
             (not s["boxStale"]) and s["sendDisabled"] is False)
    # 会话语言双侧未知 → 不过期；之后解析出另一种语言（发送会真的改译）→ 过期
    ev("() => { window.__xlResolved = ''; window.__el._effConvLang = ''; q('.preview [data-act=\"tts\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    ck.check("V28 客户语言双侧未知 → 不过期", not s["boxStale"])
    ev("() => { window.__el._effConvLang = 'ja'; window.__el.refreshXl(); }")
    s = ev("snap()")
    ck.check("V28 之后解析出不同语种（发送会改译）→ 过期属实", s["boxStale"])
    ev("() => { window.__stubClient.voiceTts = window.__origVoiceTts; window.__el._effConvLang = null; }")

    # V29（#250 N-4 D）：配置非法 → 第三种结论「⚠ 配置需修正」——生成前状态行红点 +
    # 「配置需修正」+ 详情自动展开带「去语音页修正」深链；点生成 → 服务端闸拦下
    # （reason=voice_config:*）→ 黄字 + 同一深链、无「重试」（重试只会再撞闸）、无音频。
    ev("() => { q('.preview [data-act=\"clear-preview\"]').click(); }")
    ev("""() => {
      window.__stubClient.voiceEffectiveConfig = async () => ({
        ok: true, hub_strict: false, persona_id: 'mizuki', persona_source: 'explicit',
        backend: 'avatar_clone', voice: 'ja-JP-NanamiNeural', is_clone: true, ready: false,
        hub_risk: '', conv_lang: 'zh', voice_langs: [],
        voice_config_blocked: true,
        voice_problems: [{ code: 'clone_missing_reference', detail: '', severity: 'error', blocking: true }],
        voice_problem_text: '语音配置需修正：选了「用我上传的录音」但还没有录音。请先在语音页上传参考录音',
        voice_tab: '/personas#profile=mizuki&tab=voice' });
    }""")
    ev("window.__el._refreshEffStatus()")
    page.wait_for_timeout(150)
    eff = ev("""() => {
      const box = q('[data-role="effstatus"]'); const det = q('[data-role="effdetail"]');
      const link = det ? det.querySelector('[data-role="cfg-fix"]') : null;
      return { visible: !!(box && !box.hidden && getComputedStyle(box).display !== 'none'),
               dot: box ? ((box.querySelector('.dot') || {}).className || '') : '',
               risk: box ? ((box.querySelector('[data-role="cfg-risk"]') || {}).textContent || '') : '',
               detOpen: !!(det && !det.hidden && getComputedStyle(det).display !== 'none'),
               detText: det ? det.textContent : '', href: link ? link.getAttribute('href') : '' };
    }""")
    ck.check("V29 配置非法：状态行红点 + 「配置需修正」",
             eff["visible"] and "err" in eff["dot"] and "配置需修正" in eff["risk"], str(eff))
    ck.check("V29 详情自动展开：服务端人话 + 「去语音页修正」深链（#profile=<id>&tab=voice）",
             eff["detOpen"] and "还没有录音" in eff["detText"]
             and eff["href"] == "/personas#profile=mizuki&tab=voice", str(eff))
    ev("""() => {
      window.__stubClient.voiceTts = async (args) => {
        window.__calls.tts.push(JSON.parse(JSON.stringify(args || {})));
        await new Promise((r) => setTimeout(r, 20));
        return { ok: false, reason: 'voice_config:clone_missing_reference', error: 'voice_config:clone_missing_reference',
                 message: '语音配置需修正：选了「用我上传的录音」但还没有录音。请先在语音页上传参考录音',
                 voice_problems: [{ code: 'clone_missing_reference', severity: 'error' }],
                 persona_id: 'mizuki', voice_tab: '/personas#profile=mizuki&tab=voice' };
      };
    }""")
    ev(f"() => setText({TEXT_A!r})")
    ev("() => { q('[data-role=\"gen-main\"]').click(); }")
    ev("waitIdle()")
    s = ev("snap()")
    ge = ev("""() => {
      const n = q('.preview [data-role="gen-error"]'); const link = q('.preview [data-role="cfg-fix"]');
      return { has: !!n, reason: n ? n.getAttribute('data-reason') : '', cls: n ? n.className : '',
               text: n ? n.textContent : '', href: link ? link.getAttribute('href') : '',
               retry: !!q('.preview [data-act="tts"]'), audio: !!q('.preview audio'),
               banners: visibleBanners() };
    }""")
    ck.check("V29 点生成被闸拦下：⚠ 配置需修正（黄字，服务端人话）、无音频、无「疑似无声」",
             ge["has"] and ge["reason"] == "voice_config" and "warn" in ge["cls"]
             and "配置需修正" in ge["text"] and "还没有录音" in ge["text"]
             and (not ge["audio"]) and ge["banners"] == [], str(ge))
    ck.check("V29 出路是「去语音页修正」深链而不是「重试」",
             ge["href"] == "/personas#profile=mizuki&tab=voice" and (not ge["retry"]) and s["hasClear"],
             str(ge))
    ev("() => { window.__stubClient.voiceTts = window.__origVoiceTts; delete window.__stubClient.voiceEffectiveConfig; }")


def main() -> int:
    # Windows 控制台默认 GBK：emoji/生僻字直接 UnicodeEncodeError，统一改 UTF-8 输出
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
            page = browser.new_page(viewport={"width": 480, "height": 900})
            errors: List[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # 原生 confirm 弹层（V22 降级发送确认）：按 dlg["accept"] 接/拒并计数
            dlg = {"accept": True, "count": 0, "last": ""}

            def _on_dialog(d):
                dlg["count"] += 1
                dlg["last"] = str(d.message or "")
                if dlg["accept"]:
                    d.accept()
                else:
                    d.dismiss()

            page.on("dialog", _on_dialog)
            page.goto(page_fp.as_uri())
            page.wait_for_timeout(150)   # 组件注册 + 首次渲染
            run(page, ck, dlg)
            ck.check("V0 全程零未捕获 JS 异常", not errors, "; ".join(errors[:3]))
            browser.close()
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
