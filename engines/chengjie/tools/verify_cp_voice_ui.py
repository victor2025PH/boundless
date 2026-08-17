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
window.__sendDelayMs = 30;
window.__sendFail = false;
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
    return { ok: true, audio_url: 'data:audio/mp3;base64,AAAA',
      filename: 'ttspreview-00aabbccdd.mp3',
      voice_meta: { persona_id: args && args.persona_id || '', provider: 'edge_tts',
        voice: 'zh-CN-XiaoxiaoNeural', emotion: 'warm',
        fallback_from: window.__ttsFallback ? 'avatar_clone' : '' } };
  },
  async sendVoice(body) {
    window.__calls.send.push(JSON.parse(JSON.stringify(body || {})));
    await new Promise((r) => setTimeout(r, window.__sendDelayMs));
    if (window.__sendFail) return { ok: false, message: 'peer-offline' };
    // 所听即所发：带 preview_filename 即按复用命中回显（与服务端契约同语义）
    return { ok: true, reused_preview: !!(body && body.preview_filename),
      voice_meta: { provider: 'edge_tts', emotion: 'warm' } };
  },
};
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
window.snap = () => {
  const box = window.q('[data-role="preview"]');
  const main = window.q('[data-role="gen-main"]');
  const send = window.q('[data-act="send"]');
  const note = window.q('[data-role="stale-note"]');
  const hint = window.__el.shadowRoot.querySelector('.hint');
  return {
    boxHidden: !box || box.hidden, boxHtmlLen: box ? box.innerHTML.length : 0,
    boxStale: !!(box && box.classList.contains('stale')),
    mainLabel: main ? main.textContent : '', mainDisabled: !!(main && main.disabled),
    sendLabel: send ? send.textContent : null, sendDisabled: send ? send.disabled : null,
    noteVisible: !!(note && !note.hidden),
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


def run(page, ck: Checker) -> None:
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
    ck.check("V9 提示驻留后恢复默认引导", "先生成试听" in s["hintText"], s["hintText"])

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
            page.goto(page_fp.as_uri())
            page.wait_for_timeout(150)   # 组件注册 + 首次渲染
            run(page, ck)
            ck.check("V0 全程零未捕获 JS 异常", not errors, "; ".join(errors[:3]))
            browser.close()
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
