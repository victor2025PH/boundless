#!/usr/bin/env python3
"""P 系列事件驱动录屏：客户侧真发外语来信 → 翻译 → AI 拟稿/手打 → 发出 → 克隆声试听，每步落时间戳标记。

  python record_persona.py P1
  python record_persona.py P1 --only a_inquiry

与 record_chatx 的区别：
  - 步骤是「会改变画面的动作」（incoming / xlate_quick_on / ai_reply_send / type_send / voice_preview），
    每个动作等到 DOM 真变化（气泡数增加 / 译文行出现 / 草稿面板 ready / audio src 出现）才算完成；
  - 每步写 marker {op,label,narr,zoom,t0,t1,...} 进 footage_<clip>.json，拼装按标记切、不按歌词行数切；
  - 只对 scenarios_persona.stages 里的两方会话发送（客户侧 → 卖家侧都是自家账号），不看 DEMO_PEERS；
  - 语音试听产物经 API 拉回并验 magic bytes 后落盘，拼装时混进成片（录屏本身无声）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from chatx_session import BASE, latest_video, session, token  # noqa: E402
from persona_stage import (  # noqa: E402
    clear_seller_view, customer_send, delete_stage_conversation, drop_sla_demo, end_takeover, is_ingest_stage,
    pull_group_latest, seed_sla_demo, set_automation_mode, set_outbound_lang, settle_sla_demo, sla_demo_visible,
    stage, thread_messages, wait_inbound, wx_copilot_status, wx_env, wx_green, wx_policy_get, wx_policy_restore,
)
from record_chatx import (  # noqa: E402
    _first_css, dur, inject_pointer, open_chat, point_and_maybe_click, run_steps,
)

SC = json.loads((ROOT / "scenarios_persona.json").read_text(encoding="utf-8"))
NARR_CPS = 0.19   # 念白估时：秒/字（Xiaoxiao/Yunxi -2% 实测 ≈4.8~5.3 字/秒）
CONNECTING = "正在连接"
ZOOM_WORDS = ("zoom", "zoom_list", "zoom_low", "zoom_panel", "chat", "list", "low", "panel", "list_clean", "zoom_guide")
# F2 微信引导页第 2 步真点「保存」改了老板实例的副驾档位：快照落盘，wx_restore / _cleanup / 下次开录三处兜底还原
WX_SNAPSHOT_FILE = ROOT / "out" / "_wx_policy_snapshot.json"
WX_TIER_NAMES = {"copilot": "只读建议", "semi": "半自动", "auto_reply": "全自动"}
# list_clean：会话列表已被 list_filter 过滤到只剩舞台联系人，出厂时不打马赛克（publish LIST_RECT 对应 None）
# #mode-select 真实取值（2026-09-17 探针）：manual / review / multi_choice / auto_ai
MODES = {"manual": "🙋 手动", "review": "📝 半自动（AI 草稿我审）", "multi_choice": "🔀 AI 多选我挑", "auto_ai": "🚀 全自动"}


def _now(page) -> float:
    return round(time.monotonic() - page.t_created, 2)


def _count(page, css: str) -> int:
    return int(page.evaluate("(css) => document.querySelectorAll(css).length", css))


def _wait_for(page, js: str, timeout_ms: int, poll_ms: int = 400) -> bool:
    """轮询 JS 表达式为真；不用 wait_for_function（页面常驻轮询会拖慢它）。"""
    t0 = time.monotonic()
    while (time.monotonic() - t0) * 1000 < timeout_ms:
        try:
            if page.evaluate(js):
                return True
        except Exception:
            pass
        page.wait_for_timeout(poll_ms)
    return False


def _no_connecting(page) -> bool:
    return not bool(page.evaluate("() => ((document.body&&document.body.innerText)||'').includes('%s')" % CONNECTING))


def wait_chat_ready(page, timeout_ms: int = 18000) -> bool:
    ok = _wait_for(page, """() => {
        const t=(document.body&&document.body.innerText)||'';
        if(t.includes('正在连接')) return false;
        return !!(document.querySelector('#reply-ta') || document.querySelector('.msg-row'));
    }""", timeout_ms)
    page.wait_for_timeout(700)
    return ok


def hide_staging_artifacts(page) -> None:
    """两方舞台都是自家账号 → 会话头状态带会亮红字「对方在『永不自动回复』名单（同事 / 本租户账号 …）」。
    这是排演环境的产物，真实客户会话不会出现，录制时隐藏（2026-09-18 P7 帧检发现，P1~P8 成片里也都带着）。"""
    css = "#cs-band,#cs-note{display:none!important}"
    # 客户侧账号也登在同一工作台 → 列表里会出现镜像行（客户视角的同一会话）。真实客户不在卖家工作台里，隐藏之。
    for st in SC["stages"].values():
        css += f'\n.conv-item[data-key^="{st["platform"]}:{st["customer_account"]}:"]{{display:none!important}}'
    # 微信引导页第 3 步「发一条测试消息」等待态会把该账号**最近一条真实消息**当预览行露出来（老板在测的会话）；
    # 收到测试消息后的绿卡（.got）只显示测试消息本身，不受影响。隐藏预览行是录制脱敏，不改功能。
    css += "\n.cg-recent .prev{visibility:hidden!important}"
    # F2 第三步的测试消息在 manual 档注入（不让它起一稿），e_inbox 再切 review → 会话头亮接力记忆 pill
    # 「AI 已接力（hh:mm）手动交回，记着人工阶段 1 条」。这是排演档位切换的副产物，真实接入流程里没有；只藏 pill 本体。
    css += "\n.tko-pill.handoff{display:none!important}"
    try:
        page.add_style_tag(content=css)
    except Exception:
        pass


def dom_assert(page) -> dict:
    return page.evaluate("""() => ({
        in_count: document.querySelectorAll('.msg-row.in').length,
        out_count: document.querySelectorAll('.msg-row.out').length,
        in_translated: document.querySelectorAll('.msg-row.in .msg-translation').length,
        out_translated: document.querySelectorAll('.msg-row.out .msg-translation').length,
        connecting: ((document.body&&document.body.innerText)||'').includes('正在连接'),
        empty_state: ((document.body&&document.body.innerText)||'').includes('选择一个对话'),
        header: (document.querySelector('#chat-header,.chat-header')||{}).innerText||''
    })""")


def _fetch_audio(url: str, dst_stem: Path) -> Path | None:
    """按媒体纪律：看 content-type + magic bytes 才落盘。"""
    req = urllib.request.Request(BASE + url if url.startswith("/") else url,
                                 headers={"Authorization": "Bearer " + token()})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        ctype = str(r.headers.get("Content-Type") or "")
    if "json" in ctype:
        d = json.loads(body.decode("utf-8", "replace"))
        b64 = d.get("audio_base64")
        if not b64:
            print(f"    ✗ 音频响应是 JSON 且无 audio_base64: {str(d)[:120]}")
            return None
        import base64
        body = base64.b64decode(b64)
    head = body[:12]
    if head[:4] == b"RIFF":
        ext = "wav"
    elif head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        ext = "mp3"
    elif head[:4] == b"OggS":
        ext = "ogg"
    elif head[:4] == b"fLaC":
        ext = "flac"
    elif head[4:8] == b"ftyp":
        ext = "m4a"
    else:
        print(f"    ✗ 音频 magic 不识别 {head!r} ctype={ctype}")
        return None
    dst = dst_stem.with_suffix("." + ext)
    dst.write_bytes(body)
    print(f"    ✓ 语音落盘 {dst.name} {len(body)//1024}KB ctype={ctype}")
    return dst


class ClipAbort(RuntimeError):
    """来信没到达 / 客户侧发送失败 —— 后面的翻译、AI 拟稿全是空转，立即中止本段，不出坏 take。"""


class Recorder:
    def __init__(self, page, st: dict, epdir: Path, clip: str):
        self.page, self.st, self.epdir, self.clip = page, st, epdir, clip
        self.cid = st["seller_view_conversation"]
        self.markers: list[dict] = []
        self.voice_n = 0
        self.content_offset: float | None = None

    def stage_active(self) -> bool:
        """当前打开的会话就是舞台会话（读工作台全局 selectedChat；.active class 不可靠）。"""
        return bool(self.page.evaluate(
            """(cid) => { try {
                 const a = (typeof window.__wsActiveConvId === 'function') ? window.__wsActiveConvId() : window.__wsActiveConvId;
                 if (a && String(a) === cid) return true;
                 const el = document.querySelector('.conv-item.selected[data-key="' + cid + '"], .conv-item.active[data-key="' + cid + '"]');
                 return !!el && !!document.querySelector('#reply-ta'); } catch (e) { return false; } }""",
            self.cid,
        ))

    def mark(self, op: str, label: str = "", *, t0: float, zoom: str = "", narr: str = "", **extra) -> dict:
        m = {"op": op, "label": label, "t0": t0, "t1": _now(self.page), "zoom": zoom, "narr": narr}
        m.update(extra)
        self.markers.append(m)
        return m

    # ── 动作 ────────────────────────────────────────────────────────────
    def open_stage(self, caption: str = "") -> None:
        t0 = _now(self.page)
        ok = open_chat(self.page, self.cid, caption or f"打开【{self.st['customer_display']}】会话")
        if not ok and self.page.evaluate("() => !!((document.getElementById('search-input')||{}).value)"):
            # 列表还挂着 list_filter 的搜索词，而该行最后一条已不含关键词（如 AI 已回）→ 先清搜索再点行
            self.page.evaluate("() => { const s=document.getElementById('search-input'); s.value=''; s.dispatchEvent(new Event('input',{bubbles:true})); }")
            self.page.wait_for_timeout(900)
            ok = open_chat(self.page, self.cid, caption or f"打开【{self.st['customer_display']}】会话")
        if not ok:
            # 列表里还没有（新会话）：用工作台内部入口聚焦
            self.page.evaluate("(cid) => { try{ window.__wsFocusConv && window.__wsFocusConv(cid);}catch(e){} }", self.cid)
            self.page.wait_for_timeout(1500)
        wait_chat_ready(self.page)
        if self.content_offset is None:
            self.content_offset = _now(self.page)
        self.mark("open_stage", caption, t0=t0)

    def narr(self, text: str) -> None:
        t0 = _now(self.page)
        hold = min(7.5, 0.9 + len(text) * NARR_CPS)
        self.page.wait_for_timeout(int(hold * 1000))
        self.mark("narr", "", t0=t0, narr=text)

    def say(self, caption: str, text: str) -> None:
        """字幕条 + 念白同刻起（F 系列教学片：画面说明与口播同步，不像 narr 只有声音）。
        marker 仍是 op=narr（拼装按 narr 铺 TTS），label 带字幕文本。"""
        t0 = _now(self.page)
        hold = min(9.0, 1.2 + len(text) * NARR_CPS)
        try:
            self.page.evaluate("({t,h}) => { if(window.__chatxPointer) window.__chatxPointer.caption(t,h); }",
                               {"t": caption, "h": int(hold * 1000)})
        except Exception:
            pass
        self.page.wait_for_timeout(int(hold * 1000))
        self.mark("narr", caption, t0=t0, narr=text)

    def use_stage(self, name: str) -> None:
        """多平台集（P2）：后续 incoming / open_stage / type_send 都切到另一个两方舞台（另一平台的卖家账号）。"""
        t0 = _now(self.page)
        self.st = stage(name)
        self.cid = self.st["seller_view_conversation"]
        print(f"    use_stage {name} → {self.cid}")
        self.mark("use_stage", "", t0=t0, stage=name)

    def ensure_mode(self, mode: str) -> None:
        """无镜头前置：把当前舞台会话档位钉到 mode（API 直设，不点 UI）。
        F1 主戏 / 接管段单独补录时，上一次 _cleanup 已把档位归 review——没有这一步，
        wait_outbound 会白等 90s。同一 run 内接着 d_arm 录时是幂等 no-op。"""
        t0 = _now(self.page)
        r = set_automation_mode(self.st, mode)
        print(f"    ensure_mode {mode} → {r}")
        if not r.get("ok") and not r.get("skipped"):
            raise ClipAbort(f"ensure_mode {mode} 失败: {r}")
        self.mark("ensure_mode", "", t0=t0, mode=mode)

    def conv_scope(self, scope: str, caption: str = "") -> None:
        """切私聊/群组/频道范围芯片（P6：先看【群组】，询价落地后再切回【私聊】接单）。"""
        t0 = _now(self.page)
        sel = {"group": "#ftab-scope-group", "private": "#ftab-scope-private", "channel": "#ftab-scope-channel"}.get(scope, "#ftab-scope-private")
        btn = _first_css(self.page, sel)
        point_and_maybe_click(self.page, btn, caption or f"切到【{scope}】", click=True, hold_ms=900)
        _wait_for(self.page, "() => { const el=document.querySelector('%s'); return !!(el && el.classList.contains('active')); }" % sel, 4000, 200)
        if scope == "private":
            # 群组筛选词还挂着时，私聊舞台行会被搜掉（2026-09-18 P6 take2：切私聊后搜索仍是「2026社群」）
            self.page.evaluate("() => { const s=document.getElementById('search-input'); if(s && s.value){ s.value=''; s.dispatchEvent(new Event('input',{bubbles:true})); } }")
        self.page.wait_for_timeout(600)
        print(f"    conv_scope {scope}")
        self.mark("conv_scope", caption, t0=t0, zoom="list_clean" if scope == "group" else "", scope=scope)

    def list_filter(self, query: str, caption: str = "", zoom: str = "list_clean") -> None:
        """会话列表搜索框过滤（按联系人名 / 最后一条消息文本）。演示只剩舞台联系人 → 列表可裸出镜。"""
        t0 = _now(self.page)
        # goto 后列表异步装载：先等搜索框可见且列表有行，否则得点落空、行数读 0（2026-09-18 P2 首录实锤）
        _wait_for(self.page, "() => { const s=document.getElementById('search-input'); if(!s) return false; const r=s.getBoundingClientRect(); return r.width>0 && document.querySelectorAll('.conv-item').length>0; }", 20000, 400)
        # locator('#search-input').count() 偶发读 0（2026-09-18 六录）而 getElementById 有 → 走 _first_css 多选择器兜底
        si = _first_css(self.page, "#search-input, .conv-search input, input[placeholder*='搜索联系人']")
        point_and_maybe_click(self.page, si, caption or f"搜「{query}」", click=True, hold_ms=800)
        self.page.keyboard.type(query, delay=70)
        if self.page.evaluate("() => (document.getElementById('search-input')||{}).value||''") != query:
            self.page.evaluate("(q) => { const s=document.getElementById('search-input'); if(s){ s.focus(); s.value=q; s.dispatchEvent(new Event('input',{bubbles:true})); } }", query)
        self.page.wait_for_timeout(1200)
        n = int(self.page.evaluate("() => Array.from(document.querySelectorAll('.conv-item')).filter(e => e.offsetParent !== null).length"))
        # 舞台刚清空、来信未到时 0 行是预期（过滤词命中的是「最后一条消息」）；来信落地后行会自己长出来
        print(f"    list_filter '{query}' → {n} rows")
        lst = _first_css(self.page, ".conv-list, #conv-list, .conv-item")
        point_and_maybe_click(self.page, lst, caption or f"只看「{query}」", click=False, hold_ms=1200)
        self.mark("list_filter", caption, t0=t0, zoom=zoom, query=query, rows=n)

    def kb_answer(self, query: str, pick: str, caption: str = "", zoom: str = "zoom_low", send: str = "send") -> None:
        """回复台输入 / + 关键词 → 斜杠面板列出知识库/模板 → 点中含 pick 的条目 → 答案填入 → 发送（出站自动翻译）。"""
        t0 = _now(self.page)
        before_out = _count(self.page, ".msg-row.out")
        ta = _first_css(self.page, "#reply-ta")
        point_and_maybe_click(self.page, ta, caption or "回复框输入 / 调知识库", click=True, hold_ms=800)
        self.page.evaluate("() => { const t=document.getElementById('reply-ta'); if(t){ t.value=''; t.dispatchEvent(new Event('input',{bubbles:true})); } }")
        self.page.keyboard.type("/" + query, delay=90)
        shown = _wait_for(self.page, "() => { const p=document.getElementById('slash-panel'); return !!(p && p.classList.contains('show') && document.querySelectorAll('#slash-items .slash-item').length>0); }", 12000, 300)
        self.page.wait_for_timeout(700)
        self.mark("kb_open", f"输入 /{query}", t0=t0, zoom=zoom, shown=shown)
        t0 = _now(self.page)
        item = self.page.locator("#slash-items .slash-item").filter(has_text=pick).first
        if not item.count():
            item = self.page.locator("#slash-items .slash-item").first
        point_and_maybe_click(self.page, item if item.count() else None, f"知识库命中「{pick}」", click=True, hold_ms=1300)
        filled = _wait_for(self.page, "() => { const t=document.getElementById('reply-ta'); return !!t && t.value.length>20 && !t.value.startsWith('/'); }", 6000, 200)
        print(f"    kb_answer shown={shown} filled={filled}")
        self.page.wait_for_timeout(600)
        point_and_maybe_click(self.page, ta, "答案已带入回复框", click=False, hold_ms=1800)
        self.mark("kb_pick", caption, t0=t0, zoom=zoom, pick=pick, filled=filled)
        sent = False
        if send == "preview":
            # 教学片只演「斜杠能调出来」，不发：全选删掉，回复框还原干净（后面全自动段回复框不许挂着稿）
            self.page.wait_for_timeout(600)
            try:
                ta.click(timeout=2000)
                self.page.keyboard.press("Control+A")
                self.page.keyboard.press("Delete")
            except Exception:
                pass
            self.page.evaluate("() => { const t=document.getElementById('reply-ta'); if(t && t.value){ t.value=''; t.dispatchEvent(new Event('input',{bubbles:true})); } }")
            self.page.wait_for_timeout(500)
            return
        if send and filled:
            t0 = _now(self.page)
            _wait_for(self.page, "() => { const b=document.getElementById('send-btn'); return !!b && !b.disabled; }", 8000, 300)
            btn = _first_css(self.page, "#send-btn")
            point_and_maybe_click(self.page, btn, "发送，自动翻成客户的语言", click=True, hold_ms=900)
            sent = _wait_for(self.page, "() => document.querySelectorAll('.msg-row.out').length > %d" % before_out, 40000)
            self.page.wait_for_timeout(2200)
            loc = self.page.locator(".msg-row.out .msg-bubble")
            el = loc.nth(loc.count() - 1) if loc.count() else None
            point_and_maybe_click(self.page, el, "已发出", click=False, hold_ms=1600)
            print(f"    kb_answer sent={sent}")
            self.mark("kb_send", "发送", t0=t0, zoom="zoom", sent=sent)

    def incoming(self, text: str, caption: str = "", zoom: str = "zoom") -> None:
        t0 = _now(self.page)
        chat_open = self.stage_active()
        before = _count(self.page, ".msg-row.in") if chat_open else -1
        row_js = "document.querySelector('.conv-item[data-key=\"%s\"]')" % self.cid
        row_before = self.page.evaluate("() => { const el=%s; return el ? el.innerText : null; }" % row_js) if not chat_open else None
        d = customer_send(self.st, text, tag=f"{self.clip}")
        sent_at = time.time()
        if d.get("soft") and "near_duplicate" in str(d.get("detail", "")):
            # 90 秒近重闸：同一剧本重录时被拦——加一个自然的表情尾巴再发（观众看不出区别）
            import random
            text = text + " " + random.choice(["🙂", "🙏", "👍", "😊", "✨"])
            d = customer_send(self.st, text, tag=f"{self.clip}-r")
            sent_at = time.time()
        if not d.get("ok", True) and not d.get("delivered"):
            print(f"    ✗ 客户侧发送失败: {str(d)[:160]}")
        needle = text[:18].replace("'", "\\'")
        js_row = ("() => { const el=%s; if(!el) return false; const t=el.innerText||''; return t.includes('%s') || (%s !== null && t !== %s); }"
                  % (row_js, needle, json.dumps(row_before), json.dumps(row_before)))

        # via：来信是怎么到工作台的。live=实时入站（预期）；pull=兜底补拉（2026-09-18 起引擎未触发
        # 群消息也实时镜像，再走到补拉＝入站链又坏了，不是「群就是这样」）；resend=客户侧补发。
        # 记进 marker，gate_persona 对非 live 记 WARN，别让兜底把问题吃掉。
        via = {"v": "desktop_ingest" if is_ingest_stage(self.st) else "live"}

        def _nudge_group() -> bool:
            if self.st.get("chat_type") != "group":
                return False
            via["v"] = "pull"
            print("    ! WARN 群实时入站 8s 未到，走 from_latest 兜底 —— 检查该群是否在 allowlist_chat_ids / "
                  "引擎 mirror_untriggered 是否在跑；成片会过但 gate 记 WARN")
            print("    ~ pull_group_latest →", pull_group_latest(self.st))
            landed = wait_inbound(self.st, text, 12.0, after_ts=sent_at)
            self.page.evaluate("() => { try{ typeof loadChats==='function' && loadChats(); }catch(e){} }")
            if chat_open:
                self.page.evaluate("(cid) => { try{ window.__wsFocusConv && window.__wsFocusConv(cid);}catch(e){} }", self.cid)
            return landed

        if self.st.get("chat_type") == "group":
            # 先等实时入站（@本账号应能触发）；8s 没有再 from_latest。
            # 禁止一发完就 sync-history：会把群里同一句旧询价再灌进来，画面上「都一样」。
            if not wait_inbound(self.st, text, 8.0, after_ts=sent_at):
                _nudge_group()

        if chat_open:
            ok = _wait_for(self.page, "() => document.querySelectorAll('.msg-row.in').length > %d" % before, 12000)
            if not ok:
                landed_api = wait_inbound(self.st, text, 8.0, after_ts=sent_at)
                if not landed_api:
                    landed_api = _nudge_group()
                if not landed_api:
                    d = customer_send(self.st, text, tag=f"{self.clip}-again")
                    via["v"] = "resend"
                    print(f"    ~ 补发: ok={d.get('ok')} delivered={d.get('delivered')}")
                    _nudge_group()
                ok = _wait_for(self.page, "() => document.querySelectorAll('.msg-row.in').length > %d" % before, 30000)
            target = ".msg-row.in:last-of-type .msg-bubble, .msg-row.in:last-child .msg-bubble, .msg-row.in .msg-bubble"
        else:
            # 列表行：含来信前缀 或 行文本相对发送前发生变化（WA 行预览格式与 TG 不同，2026-09-18 P2 首录 35s 未命中）
            ok = _wait_for(self.page, js_row, 12000)
            if not ok:
                landed_api = wait_inbound(self.st, text, 20.0, after_ts=sent_at)
                print(f"    ~ 列表 12s 未见行，后端落库={landed_api}")
                if not landed_api:
                    landed_api = _nudge_group()
                if not landed_api:
                    d = customer_send(self.st, text, tag=f"{self.clip}-again")
                    via["v"] = "resend"
                    print(f"    ~ 补发: ok={d.get('ok')} delivered={d.get('delivered')}")
                    if self.st.get("chat_type") == "group":
                        _nudge_group()
                self.page.evaluate("() => { try{ typeof loadChats==='function' && loadChats(); }catch(e){} try{ const s=document.getElementById('search-input'); if(s && s.value) s.dispatchEvent(new Event('input',{bubbles:true})); }catch(e){} }")
                if is_ingest_stage(self.st):
                    # 注入舞台：SSE 只增量刷已有行，新会话行要等列表周期轮询（~15s）才长出来——
                    # 等行真出现再得点，别拿「后端已落库」顶替（2026-09-19 F2 首录：得点目标未找到）
                    ok = _wait_for(self.page, js_row, 30000) or landed_api
                else:
                    ok = landed_api or _wait_for(self.page, js_row, 30000)
            target = f'.conv-item[data-key="{self.cid}"]'
        print(f"    incoming landed={ok} chat_open={chat_open} via={via['v']}")
        if not ok:
            self.mark("incoming", caption, t0=t0, zoom=zoom, text=text, landed=False, via=via["v"])
            raise ClipAbort(f"incoming 未到达（{self.st.get('customer_display')} → {self.cid}）")
        self.page.wait_for_timeout(600)
        el = None
        if chat_open:
            loc = self.page.locator(".msg-row.in .msg-bubble")
            if loc.count():
                el = loc.nth(loc.count() - 1)
        else:
            el = _first_css(self.page, target)
        point_and_maybe_click(self.page, el, caption or "客户来信", click=False, hold_ms=1800)
        self.mark("incoming", caption, t0=t0, zoom=zoom, text=text, landed=ok, via=via["v"])

    def xlate_quick_on(self, caption: str = "", zoom: str = "zoom") -> None:
        t0 = _now(self.page)
        already = self.page.evaluate("() => { const s=document.getElementById('xlate-in'); return !!(s && s.value); }")
        btn = _first_css(self.page, "#xlate-toggle-btn")
        point_and_maybe_click(self.page, btn, caption or "打开【翻译】", click=True, hold_ms=1100)
        _wait_for(self.page, "() => { const p=document.getElementById('xlate-pop'); return !!(p && p.classList.contains('show')); }", 5000)
        self.page.wait_for_timeout(500)
        if not already:
            q = _first_css(self.page, "#xl-quick-btn")
            point_and_maybe_click(self.page, q, "一键开启双向翻译", click=True, hold_ms=1200)
        else:
            q = _first_css(self.page, "#xl-quick-btn")
            point_and_maybe_click(self.page, q, "双向翻译已开：对方→中文，我→客户的语言", click=False, hold_ms=1300)
        ok = _wait_for(self.page, "() => document.querySelectorAll('.msg-row.in .msg-translation:not(.xl-skel)').length > 0", 25000)
        print(f"    xlate translated={ok} already={already}")
        self.page.wait_for_timeout(400)
        x = _first_css(self.page, "#xl-pop-x")
        if x is not None:
            try:
                x.click(timeout=2000)
            except Exception:
                self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(500)
        loc = self.page.locator(".msg-row.in .msg-translation")
        el = loc.nth(loc.count() - 1) if loc.count() else None
        point_and_maybe_click(self.page, el, "对方的话→中文，译文就在气泡下", click=False, hold_ms=1800)
        self.mark("xlate_quick_on", caption, t0=t0, zoom=zoom, translated=ok)

    def ai_reply_send(self, caption: str = "", zoom: str = "zoom") -> None:
        t0 = _now(self.page)
        before_out = _count(self.page, ".msg-row.out")
        btn = _first_css(self.page, "#ai-reply-btn")
        point_and_maybe_click(self.page, btn, caption or "点【AI回复】", click=True, hold_ms=1100)
        ready = _wait_for(self.page, "() => { const m=document.getElementById('dpick-modal'); return !!(m && m.getAttribute('data-st')==='ready' && document.querySelectorAll('#dpick-list .dpick-row, #dpick-list li, #dpick-list [data-idx], #dpick-list > *').length>0); }", 75000, 500)
        print(f"    dpick ready={ready}")
        self.page.wait_for_timeout(900)
        lst = _first_css(self.page, "#dpick-list")
        point_and_maybe_click(self.page, lst, "AI 按人设拆成短句，勾选即可", click=False, hold_ms=2000)
        send = _first_css(self.page, "#dpick-send-btn")
        point_and_maybe_click(self.page, send, "逐条发送", click=True, hold_ms=1000)
        sent = _wait_for(self.page, "() => document.querySelectorAll('.msg-row.out').length > %d" % before_out, 45000)
        self.page.wait_for_timeout(800)
        # 气泡先出来再「发送中」：等它落地，否则尾帧字幕写「已发出」画面还转圈（P6 take2）
        delivered = _wait_for(self.page, """() => {
            const rows=[...document.querySelectorAll('.msg-row.out')];
            if(!rows.length) return false;
            const t=rows[rows.length-1].innerText||'';
            return !t.includes('发送中') && !t.includes('发送失败');
        }""", 18000, 400)
        failed = self._recover_failed_out()
        drained = self._drain_reply_queue()
        print(f"    ai reply sent={sent} delivered={delivered} out {before_out}→{_count(self.page, '.msg-row.out')} failed_left={failed} drained={drained}")
        if failed:
            raise ClipAbort("AI 回复发送失败且重发未恢复（平台通道瞬断）—— 画面里挂着红字「发送失败」，弃 take")
        loc = self.page.locator(".msg-row.out .msg-bubble")
        el = loc.nth(loc.count() - 1) if loc.count() else None
        point_and_maybe_click(self.page, el, "已发出，自动翻成客户的语言", click=False, hold_ms=1800)
        self.mark("ai_reply_send", caption, t0=t0, zoom=zoom, ready=ready, sent=sent and not failed)

    # ── F 系列：全自动真发 ───────────────────────────────────────────────
    _OUT_TEXTS_JS = "() => [...document.querySelectorAll('.msg-row.out')].map(r => (r.innerText||'').trim())"

    def _api_outs_after(self, after_ts: float) -> list[dict]:
        """卖家侧 thread 里 after_ts 之后的出站（引擎真发的事实源：text=实发译文，agent_original=中文原稿）。"""
        out = []
        for m in thread_messages(self.st, limit=30):
            if m.get("direction") != "out":
                continue
            if float(m.get("ts") or 0) < after_ts - 2:
                continue
            out.append({"text": str(m.get("text") or "")[:300], "original": str(m.get("agent_original") or m.get("original_text") or "")[:300],
                        "sent_by": m.get("sent_by"), "status": m.get("status"), "ts": m.get("ts"), "language": m.get("language")})
        return out

    def wait_outbound(self, caption: str = "", zoom: str = "zoom", timeout_s: float = 150.0, settle_s: float = 16.0) -> None:
        """等引擎自己把回复发出去——本原语**不点任何按钮**（这是全自动教学片的全部意义）。
        等到第一条新出站气泡后，再给 settle_s 等人设拆的后续短句；全程没有出站 → ClipAbort（主戏不成立，不出坏 take）。
        marker：ok / elapsed / n_new / texts（气泡正文，含中文原稿副行）/ api（thread 真值：实发译文 + 原稿 + sent_by）。"""
        t0 = _now(self.page)
        started = time.time()
        before_out = _count(self.page, ".msg-row.out")
        anchor = _first_css(self.page, "#reply-ta") or _first_css(self.page, "#chat-header, .chat-header")
        point_and_maybe_click(self.page, anchor, caption or "没有人点发送：AI 按人设自己回", click=False, hold_ms=2200)
        # 手不许碰：把鼠标停在画面外侧，观众看得出没有点击
        try:
            self.page.mouse.move(1880, 1000)
        except Exception:
            pass
        got = _wait_for(self.page, "() => document.querySelectorAll('.msg-row.out').length > %d" % before_out, int(timeout_s * 1000), 500)
        first_at = round(time.time() - started, 1)
        if got:
            # 人设「每次不超过 4 句」常拆成 2 条：等后续短句落地（连续 settle_s 秒无新增即算稳）
            last_n = _count(self.page, ".msg-row.out")
            quiet_since = time.time()
            while time.time() - quiet_since < settle_s and time.time() - started < timeout_s + settle_s:
                self.page.wait_for_timeout(700)
                n = _count(self.page, ".msg-row.out")
                if n != last_n:
                    last_n, quiet_since = n, time.time()
            _wait_for(self.page, """() => { const rows=[...document.querySelectorAll('.msg-row.out')]; if(!rows.length) return false;
                const t=rows[rows.length-1].innerText||''; return !t.includes('发送中') && !t.includes('发送失败'); }""", 15000, 400)
        n_new = _count(self.page, ".msg-row.out") - before_out
        texts = self.page.evaluate(self._OUT_TEXTS_JS)[-max(n_new, 0):] if n_new > 0 else []
        api = self._api_outs_after(started)
        print(f"    wait_outbound ok={got} first_at={first_at}s n_new={n_new} api={len(api)} sent_by={[a.get('sent_by') for a in api]}")
        for a in api:
            print(f"      out: {a['text'][:90]!r}  <- {a['original'][:60]!r}")
        if not got:
            self.mark("wait_outbound", caption, t0=t0, zoom=zoom, ok=False, elapsed=first_at, n_new=0, texts=[], api=api, clicked_send=False)
            raise ClipAbort(f"wait_outbound {timeout_s:.0f}s 内无自动出站（查 hub 日志 peer-guard / AutoDraft / autosend 原因）")
        loc = self.page.locator(".msg-row.out .msg-bubble")
        el = loc.nth(loc.count() - 1) if loc.count() else None
        point_and_maybe_click(self.page, el, "发出去的是客户的语言，中文原稿在下面", click=False, hold_ms=2600)
        self.mark("wait_outbound", caption, t0=t0, zoom=zoom, ok=True, elapsed=first_at, n_new=n_new, texts=texts, api=api, clicked_send=False)

    def click_takeover(self, caption: str = "", zoom: str = "zoom") -> None:
        """会话头【接管】（/api/takeover/start：档位切 manual + 取消在途草稿 + 打标签）。window.confirm 由
        _record_clips 的 dialog 处理器直接接受。按钮不在（接管后端未就绪）→ 退回 mode_switch manual（P8 路径）。"""
        t0 = _now(self.page)
        btn = _first_css(self.page, "#tko-btn")
        visible = False
        if btn is not None:
            try:
                visible = bool(btn.is_visible())
            except Exception:
                visible = False
        if not visible:
            print("    ~ #tko-btn 不可见，退回档位下拉切手动")
            self.mode_switch("manual", caption or "切【手动】：AI 让位", zoom)
            self.markers[-1]["op"] = "click_takeover"
            self.markers[-1]["fallback"] = "mode_switch"
            return
        point_and_maybe_click(self.page, btn, caption or "点【接管】：AI 对这条会话立刻让位", click=True, hold_ms=1300)
        # 服务端 start_takeover 已把档位切 manual（takeover.py）；前端按钮标签立刻变「交还 AI · 0m」，
        # #mode-select 要等下一轮会话轮询才刷成 manual——两者任一即算接管成立，再多等几秒让下拉也跟上
        ok = _wait_for(self.page, "() => { const s=document.getElementById('mode-select'); const l=document.getElementById('tko-btn-lbl'); "
                                  "return (!!s && s.value==='manual') || (!!l && !/^接管$/.test((l.textContent||'').trim())); }", 12000, 300)
        _wait_for(self.page, "() => { const s=document.getElementById('mode-select'); return !!s && s.value==='manual'; }", 6000, 300)
        self.page.wait_for_timeout(800)
        mode = self.page.evaluate("() => { const s=document.getElementById('mode-select'); return s ? s.value : null; }")
        lbl = self.page.evaluate("() => { const l=document.getElementById('tko-btn-lbl'); return l ? (l.textContent||'').trim() : null; }")
        print(f"    click_takeover ok={ok} mode={mode} lbl={lbl!r}")
        point_and_maybe_click(self.page, _first_css(self.page, "#chat-header, .chat-header"), "本会话：手动 · AI 已让位", click=False, hold_ms=1800)
        self.mark("click_takeover", caption, t0=t0, zoom=zoom, ok=bool(ok), mode=mode, btn_label=lbl)

    def assert_quiet(self, seconds: int = 8, caption: str = "", zoom: str = "zoom") -> None:
        """接管后对照：等 seconds 秒，出站气泡数不再增加（AI 没插话）。"""
        t0 = _now(self.page)
        before = _count(self.page, ".msg-row.out")
        anchor = _first_css(self.page, ".msg-row.out:last-child .msg-bubble") or _first_css(self.page, "#chat-header, .chat-header")
        point_and_maybe_click(self.page, anchor, caption or f"AI 让位后 {seconds} 秒：没有新气泡", click=False, hold_ms=int(seconds) * 1000)
        after = _count(self.page, ".msg-row.out")
        quiet = after == before
        print(f"    assert_quiet {seconds}s out {before}→{after} quiet={quiet}")
        self.mark("assert_quiet", caption, t0=t0, zoom=zoom, seconds=int(seconds), quiet=quiet)

    def ai_diag(self, caption: str = "", zoom: str = "zoom") -> None:
        """会话头【🩺 AI 状态】：这条会话 AI 会不会自动回、不回为什么（全自动档顶部还有「全自动运行中」卡）。看 3.5s 后关。"""
        t0 = _now(self.page)
        btn = _first_css(self.page, "#ai-diag-btn")
        point_and_maybe_click(self.page, btn, caption or "点【AI 状态】", click=True, hold_ms=1100)
        shown = _wait_for(self.page, "() => { const o=document.getElementById('ai-diag-overlay'); return !!o && o.style.display!=='none' && (o.innerText||'').trim().length>20; }", 12000, 300)
        self.page.wait_for_timeout(600)
        head = self.page.evaluate("() => { const o=document.getElementById('ai-diag-overlay'); return o ? (o.innerText||'').trim().slice(0,160) : ''; }")
        print(f"    ai_diag shown={shown} head={head[:100]!r}")
        card = _first_css(self.page, "#ai-diag-overlay .auto-safety-bar, #auto-safety-bar") if shown else None
        try:
            card_visible = bool(card is not None and card.is_visible())
        except Exception:
            card_visible = False
        el = card if card_visible else _first_css(self.page, "#ai-diag-overlay > div, #ai-diag-overlay")
        point_and_maybe_click(self.page, el, "AI 会不会回、为什么不回，一眼看懂", click=False, hold_ms=3500)
        try:
            self.page.keyboard.press("Escape")
        except Exception:
            pass
        self.page.wait_for_timeout(300)
        if self.page.evaluate("() => { const o=document.getElementById('ai-diag-overlay'); return !!o && o.style.display!=='none'; }"):
            self.page.evaluate("() => { const o=document.getElementById('ai-diag-overlay'); if(o) o.style.display='none'; }")
        self.page.wait_for_timeout(400)
        self.mark("ai_diag", caption, t0=t0, zoom=zoom, shown=shown, safety_bar=card_visible, head=head[:120])

    # ── F2 微信接入教学：引导页 /workspace/connect/wechat_pc 三步 + 收件箱拟稿人审 ──────────────
    def goto_page(self, path: str, wait_css: str = "#cg-panel, #reply-ta, .conv-item", caption: str = "") -> None:
        """整页跳转（引导页 / 工作台）。与静态 goto 的区别：不等聊天就绪（引导页没有会话，等 12s 全是空转），
        等 wait_css 之一出现即可；跳转后补注指针与脱敏样式。"""
        t0 = _now(self.page)
        from chatx_session import url as _url
        self.page.goto(_url(path), wait_until="domcontentloaded", timeout=60000)
        _wait_for(self.page, "() => !!document.querySelector(%s)" % json.dumps(wait_css), 20000, 300)
        self.page.wait_for_timeout(1500)
        inject_pointer(self.page)
        hide_staging_artifacts(self.page)
        if self.content_offset is None:
            self.content_offset = _now(self.page)
        print(f"    goto_page {path} → {self.page.url.split('?')[0][-40:]}")
        self.mark("goto_page", caption, t0=t0, path=path)

    def _cg_step(self, n: int) -> None:
        """引导页步骤栏第 n 步（1..3）。首屏会自动落在「第一个未完成步」，录制要按剧本顺序走。"""
        cur = self.page.evaluate("() => ((document.getElementById('cg-step-k')||{}).textContent||'')")
        if f"第 {n} 步" in cur:
            return
        btn = _first_css(self.page, f"#cg-rail button:nth-of-type({n})")
        point_and_maybe_click(self.page, btn, None, click=True, hold_ms=500)
        _wait_for(self.page, "() => ((document.getElementById('cg-step-k')||{}).textContent||'').includes('第 %d 步')" % n, 6000, 200)
        self.page.wait_for_timeout(800)

    def cg_step(self, n: int) -> None:
        """无镜头：把引导页钉到第 n 步。首屏环境全绿 1.6s 后页面会自动跳到第 2 步（connect_guide
        autoAdvanced，每次加载只跳一次）——2026-09-19 b_connect 首录：念「第一步」时面板已经是第 2 步。
        先让它跳完再点回来，之后念白期间面板不再自己换步。"""
        t0 = _now(self.page)
        self.page.wait_for_timeout(2600)
        self._cg_step(int(n))
        cur = self.page.evaluate("() => ((document.getElementById('cg-step-k')||{}).textContent||'').trim()")
        print(f"    cg_step {n} → {cur!r}")
        self.mark("cg_step", "", t0=t0, step=int(n), cur=cur)

    def wx_env(self, caption: str = "", zoom: str = "zoom_guide") -> None:
        """第一步「连接电脑微信」：等环境卡渲染，API 复核绿灯（跑 / 主窗口可见 / 版本 / 驱动 / 副驾 online）。
        红灯直接 ClipAbort——老板把微信收进托盘或兄弟线在重启副驾时，卡上是黄色告警，录进成片就是坏 take。"""
        t0 = _now(self.page)
        self._cg_step(1)
        _wait_for(self.page, "() => { const e=document.getElementById('cg-env'); return !!e && (e.innerText||'').includes('版本'); }", 15000, 300)
        env = wx_env()
        ok, why = wx_green(env, wx_copilot_status())
        txt = self.page.evaluate("() => ((document.getElementById('cg-env')||{}).innerText||'').replace(/\\n+/g,' | ')")
        print(f"    wx_env green={ok} why={why} card={txt[:120]!r}")
        if not ok:
            self.mark("wx_env", caption, t0=t0, zoom=zoom, ok=False, why=why)
            raise ClipAbort(f"微信环境红灯 {why}（电脑微信主窗口不可见 / 副驾不在线），不录")
        if "!" in txt or "不可见" in txt or "未登录" in txt:
            self.mark("wx_env", caption, t0=t0, zoom=zoom, ok=False, why=["card_warn"], card=txt[:120])
            raise ClipAbort(f"引导页环境卡带告警：{txt[:80]}")
        card = _first_css(self.page, "#cg-env")
        point_and_maybe_click(self.page, card, caption or "环境体检：电脑微信在跑、版本 4.x、已登录", click=False, hold_ms=2600)
        self.mark("wx_env", caption, t0=t0, zoom=zoom, ok=True, version=env.get("version"), card=txt[:120])

    def wx_tier(self, tier: str = "semi", caption: str = "", zoom: str = "zoom_guide") -> None:
        """第二步「设置副驾」：先快照老板现档位（落盘），点档位卡 → 保存 → 等「当前：X」翻过来 + API 复核。
        全自动档需勾知情同意，教学片不选它（选半自动：只发工作台批准的）。"""
        t0 = _now(self.page)
        self._cg_step(2)
        _wait_for(self.page, "() => document.querySelectorAll('#cg-tiers label[data-tier]').length>=3", 12000, 300)
        snap = wx_policy_get()
        snapshot = {k: snap.get(k) for k in ("tier", "work_hours", "risk_ack")}
        if not WX_SNAPSHOT_FILE.exists():
            WX_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
            WX_SNAPSHOT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        print(f"    wx_tier snapshot={snapshot} → pick {tier}")
        cards = _first_css(self.page, "#cg-tiers")
        point_and_maybe_click(self.page, cards, "三档：只读建议 / 半自动 / 全自动", click=False, hold_ms=2200)
        card = _first_css(self.page, f"#cg-tiers label[data-tier='{tier}']")
        point_and_maybe_click(self.page, card, caption or f"选【{WX_TIER_NAMES.get(tier, tier)}】", click=True, hold_ms=1200)
        save = _first_css(self.page, "#cg-save-tier")
        point_and_maybe_click(self.page, save, "保存，立即生效", click=True, hold_ms=900)
        name = WX_TIER_NAMES.get(tier, tier)
        # 保存成功后状态位文案是「已保存，立即生效」（cg.pc.tier_saved），不是「当前：X」；失败是红字 cg-status bad
        saved_ui = _wait_for(self.page, "() => { const e=document.getElementById('cg-tier-st'); if(!e) return false; const t=e.textContent||''; return (t.includes('已保存') || t.includes(%s)) && !e.classList.contains('bad'); }" % json.dumps(name), 10000, 300)
        cur = wx_policy_get()
        saved = bool(cur.get("tier") == tier)
        st_txt = self.page.evaluate("() => ((document.getElementById('cg-tier-st')||{}).textContent||'').trim()")
        print(f"    wx_tier saved_ui={saved_ui} api_tier={cur.get('tier')} saved={saved} st={st_txt!r}")
        self.page.wait_for_timeout(800)
        st_el = _first_css(self.page, "#cg-tier-st")
        point_and_maybe_click(self.page, st_el, st_txt or f"已保存：{name}", click=False, hold_ms=1500)
        self.mark("wx_tier", caption, t0=t0, zoom=zoom, tier=tier, saved=saved, saved_ui=saved_ui, prev=snapshot.get("tier"))

    def wx_verify(self, caption: str = "", zoom: str = "zoom_guide", timeout_s: float = 25.0) -> None:
        """第三步「启动并验证」：副驾状态卡须是在线（不是 blind「看不到微信窗口」/ offline）。副驾本来就在跑
        （老板实例常驻）→ 卡直接是在线；不在线不点「启动」（会打断老板在跑的副驾），ClipAbort。"""
        t0 = _now(self.page)
        self._cg_step(3)
        _wait_for(self.page, "() => { const e=document.getElementById('cg-live-ttl'); return !!e && (e.textContent||'').trim().length>0; }", 15000, 300)
        deadline = time.monotonic() + timeout_s
        online = False
        ttl = ""
        while time.monotonic() < deadline:
            ttl = self.page.evaluate("() => ((document.getElementById('cg-live-ttl')||{}).textContent||'').trim()")
            api = wx_copilot_status()
            if api.get("state") == "online" and "在线" in ttl and "看不到" not in ttl:
                online = True
                break
            self.page.wait_for_timeout(1500)
        print(f"    wx_verify online={online} ttl={ttl!r}")
        if not online:
            self.mark("wx_verify", caption, t0=t0, zoom=zoom, online=False, ttl=ttl[:80])
            raise ClipAbort(f"副驾未在线：{ttl[:60]}（不点启动，避免打断老板在跑的副驾）")
        live = _first_css(self.page, "#cg-live")
        point_and_maybe_click(self.page, live, caption or "副驾在线：开始同步电脑微信的新消息", click=False, hold_ms=2600)
        self.mark("wx_verify", caption, t0=t0, zoom=zoom, online=True, ttl=ttl[:80])

    def wx_test_inbound(self, text: str, caption: str = "", zoom: str = "zoom_guide") -> None:
        """第三步「发一条测试消息」：客户来信（舞台 customer_send → 注入舞台走副驾入站桥）→ 等 #cg-recent 翻成
        「收到了 ✓ 小林：…」绿卡。marker 记为 op=incoming（gate 的「来信在镜头里到达」同一口径），via=desktop_ingest。"""
        t0 = _now(self.page)
        box = _first_css(self.page, "#cg-recent")
        point_and_maybe_click(self.page, box, caption or "用另一个微信号给这个号发一句话", click=False, hold_ms=1500)
        d = customer_send(self.st, text, tag=f"{self.clip}")
        via = d.get("via") or ("live" if not is_ingest_stage(self.st) else "desktop_ingest")
        needle = text[:14]
        # 绿卡是 #cg-recent 容器里 innerHTML 换成的 <div class="cg-recent got">（容器自身 class 不变），轮询 6s 一拍
        landed = _wait_for(self.page, "() => { const e=document.getElementById('cg-recent'); return !!e && !!e.querySelector('.cg-recent.got') && (e.innerText||'').includes(%s); }" % json.dumps(needle), 30000, 500)
        print(f"    wx_test_inbound ok={d.get('ok')} landed={landed} via={via}")
        if not landed:
            self.mark("incoming", caption, t0=t0, zoom=zoom, text=text, landed=False, via=via)
            raise ClipAbort("测试消息 25s 未出现在引导页「发一条测试消息」卡（入站桥 / 轮询异常）")
        self.page.wait_for_timeout(600)
        got = _first_css(self.page, "#cg-recent")
        point_and_maybe_click(self.page, got, "收到了：几秒后出现在这里", click=False, hold_ms=2200)
        self.mark("incoming", caption, t0=t0, zoom=zoom, text=text, landed=True, via=via)

    def wx_restore(self) -> None:
        """无镜头：把副驾档位还原到 wx_tier 的快照（老板实例是全自动，教学片临时切了半自动）。"""
        t0 = _now(self.page)
        r = restore_wx_policy()
        print(f"    wx_restore → {r}")
        self.mark("wx_restore", "", t0=t0, restored=bool(r.get("ok")), tier=r.get("tier"))

    def wait_draft(self, caption: str = "", zoom: str = "zoom_low", timeout_s: float = 75.0) -> None:
        """拟稿人审（review）：来信后 AI 自动出草稿 → 回复框上方 #cdraft-bar「AI 草稿已就绪 · 本会话设为人审」。
        等它长出来并有正文；同时读 API 草稿正文（gate 核口径数字）。没人点发送，稿只在框上等。"""
        t0 = _now(self.page)
        deadline = time.monotonic() + timeout_s
        body = ""
        api_text, level = "", ""
        pending_since: float | None = None
        nudged = False
        q = urllib.parse.urlencode({"conversation_id": self.cid, "status": "pending"})

        def _api_pending() -> tuple[str, str]:
            try:
                ds = _api("GET", "/api/drafts?" + q).get("drafts") or []
            except Exception as e:  # noqa: BLE001
                print(f"    ~ 读草稿 API 失败: {str(e)[:80]}")
                return "", ""
            return (str(ds[0].get("draft_text") or ""), str(ds[0].get("autopilot_level") or "")) if ds else ("", "")

        while time.monotonic() < deadline:
            body = self.page.evaluate("() => { const b=document.getElementById('cdraft-bar'); const t=document.getElementById('cdraft-body'); if(!b||!t||b.offsetParent===null) return ''; return (t.innerText||'').trim(); }")
            if len(body) >= 4:
                break
            if not nudged:
                # 草稿先停泊 enriching，人设正文落成 pending 时引擎发 draft_ready（2026-09-19 修），前端据此刷草稿条。
                # 万一前端没刷（引擎未重启到含该事件的版本）：API 已 pending 超 6s 仍没条 → 无痕重开会话刷一次，
                # marker 记 nudged，gate 记 WARN——别让兜底把「草稿条不会自己长出来」的产品缺陷吃掉。
                if not api_text:
                    api_text, level = _api_pending()
                    if api_text:
                        pending_since = time.monotonic()
                elif pending_since and time.monotonic() - pending_since > 6.0:
                    self.page.evaluate("(cid) => { const el=document.querySelector('.conv-item[data-key=\"'+cid+'\"]'); if(el) el.click(); }", self.cid)
                    nudged = True
                    print("    ! WARN 草稿 API 已 pending 6s 但草稿条未现，重开会话刷新（draft_ready 事件没到前端？）")
            self.page.wait_for_timeout(800)
        if not api_text:
            api_text, level = _api_pending()
        ok = len(body) >= 4
        print(f"    wait_draft ok={ok} level={level} nudged={nudged} body={body[:60]!r}")
        if not ok:
            self.mark("wait_draft", caption, t0=t0, zoom=zoom, ok=False, nudged=nudged)
            raise ClipAbort(f"{timeout_s:.0f}s 内没等到 AI 草稿条（#cdraft-bar）")
        bar = _first_css(self.page, "#cdraft-body, #cdraft-bar")
        point_and_maybe_click(self.page, bar, caption or "AI 按人设拟好稿，等你过目", click=False, hold_ms=3200)
        self.mark("wait_draft", caption, t0=t0, zoom=zoom, ok=True, text=api_text or body, ui_text=body[:200], level=level, nudged=nudged)

    def adopt_draft(self, caption: str = "", zoom: str = "zoom_low") -> None:
        """点草稿条【采用】：稿填进回复框（可改）。不点发送——F2 承诺「AI 只写稿，发不发你定」。"""
        t0 = _now(self.page)
        btn = self.page.locator("#cdraft-bar .cdraft-btn").filter(has_text="采用").first
        point_and_maybe_click(self.page, btn if btn.count() else None, caption or "【采用】填进回复框，改两个字再发", click=True, hold_ms=1200)
        filled = _wait_for(self.page, "() => { const t=document.getElementById('reply-ta'); return !!t && t.value.trim().length>=4; }", 6000, 200)
        print(f"    adopt_draft filled={filled}")
        ta = _first_css(self.page, "#reply-ta")
        point_and_maybe_click(self.page, ta, "稿在框里，改不改、发不发，你定", click=False, hold_ms=2200)
        self.mark("adopt_draft", caption, t0=t0, zoom=zoom, filled=filled)

    _FAILED_OUT_JS = "() => document.querySelectorAll('.msg-row.out .tiny-btn[onclick^=\"resend\"]').length"

    def _recover_failed_out(self, tries: int = 2) -> int:
        """出站气泡带红字「发送失败 · 重发」（WhatsApp 通道瞬断「服务未启用」，2026-09-18 P2 六录实锤；
        同刻 API 直发却是 ok）→ 点【重发】等它恢复；返回仍失败的条数。"""
        n = int(self.page.evaluate(self._FAILED_OUT_JS))
        for _ in range(tries):
            if not n:
                break
            print(f"    ~ {n} 条出站发送失败，点重发")
            self.page.wait_for_timeout(2500)
            btn = self.page.locator('.msg-row.out .tiny-btn[onclick^="resend"]').first
            point_and_maybe_click(self.page, btn if btn.count() else None, "重发", click=True, hold_ms=600)
            _wait_for(self.page, "() => document.querySelectorAll('.msg-row.out .tiny-btn[onclick^=\"resend\"]').length===0", 20000, 500)
            self.page.wait_for_timeout(2000)
            n = int(self.page.evaluate(self._FAILED_OUT_JS))
        return n

    def _drain_reply_queue(self, stall_ms: int = 12000) -> bool:
        """「逐条发送」把后续短句排队写回回复框逐条发；首条失败会让队列停在回复框里（六录实锤）。
        等队列自然排空；停滞超 stall_ms 且发送键可点 → 手动点一次发送。返回回复框最终是否为空。"""
        js_empty = "() => { const t=document.getElementById('reply-ta'); return !t || !t.value.trim(); }"
        if _wait_for(self.page, js_empty, stall_ms, 500):
            return True
        before = _count(self.page, ".msg-row.out")
        sb = _first_css(self.page, "#send-btn")
        print("    ~ 逐条队列停滞，手动补发余下短句")
        point_and_maybe_click(self.page, sb, "发送余下短句", click=True, hold_ms=700)
        _wait_for(self.page, "() => document.querySelectorAll('.msg-row.out').length > %d" % before, 30000)
        self.page.wait_for_timeout(2000)
        return bool(_wait_for(self.page, js_empty, 8000, 500))

    def type_send(self, text: str, caption: str = "", zoom: str = "zoom") -> None:
        t0 = _now(self.page)
        active_ok = self.stage_active()
        before_out = _count(self.page, ".msg-row.out")
        ta = _first_css(self.page, "#reply-ta")
        point_and_maybe_click(self.page, ta, caption or "在回复框手打中文", click=True, hold_ms=900)
        self.page.keyboard.type(text, delay=45)
        self.page.wait_for_timeout(500)
        # 档位切换等操作会重渲染编辑区，键盘输入可能落空 → 校验后用 input 事件补写
        if not self.page.evaluate("() => { const t=document.getElementById('reply-ta'); return !!t && t.value.trim().length>0; }"):
            self.page.evaluate(
                """(v) => { const t = document.getElementById('reply-ta'); if (!t) return;
                     t.focus(); t.value = v; t.dispatchEvent(new Event('input', {bubbles: true})); }""", text)
            self.page.wait_for_timeout(600)
            print("    ~ 输入框为空，已用 input 事件补写")
        _wait_for(self.page, "() => { const b=document.getElementById('send-btn'); return !!b && !b.disabled; }", 8000, 300)
        if not active_ok:
            print("    ✗ 当前会话不是舞台会话，拒发")
            self.mark("type_send", caption, t0=t0, zoom=zoom, sent=False)
            return
        sb = _first_css(self.page, "#send-btn")
        point_and_maybe_click(self.page, sb, "点【发送】", click=True, hold_ms=800)
        sent = _wait_for(self.page, "() => document.querySelectorAll('.msg-row.out').length > %d" % before_out, 30000)
        if not sent:
            self.page.keyboard.press("Enter")
            sent = _wait_for(self.page, "() => document.querySelectorAll('.msg-row.out').length > %d" % before_out, 15000)
        self.page.wait_for_timeout(2500)
        print(f"    type_send sent={sent}")
        loc = self.page.locator(".msg-row.out .msg-bubble")
        el = loc.nth(loc.count() - 1) if loc.count() else None
        point_and_maybe_click(self.page, el, "发出去的是客户的语言", click=False, hold_ms=1600)
        self.mark("type_send", caption, t0=t0, zoom=zoom, text=text, sent=sent)

    def voice_preview(self, text: str, caption: str = "", zoom: str = "zoom", send: str = "") -> None:
        """右栏业务助手 → 工具箱 → 「语音克隆 / 发送」卡（open shadow：textarea / button.gen-btn / audio / 发送）。
        composer 的「语音 ▾」入口已被产品隐藏（B26），官方路径就是这张卡。"""
        # 分四个相位落标记（open / type / gen / send），拼装才能按相位切景、剪卡顿、对齐克隆声
        t0 = _now(self.page)
        run_steps(self.page, [["ensure_assist"], ["cp_tab", "工具箱", "语音在【工具箱】"], ["wait", 600],
                              ["expand_card", "voice", "展开【语音克隆 / 发送】"]], allow_send=False)
        self.mark("voice_open", "工具箱 → 语音克隆 / 发送", t0=t0, zoom="zoom_panel")
        t0 = _now(self.page)
        ta = self.page.locator("#ws-cp-voice textarea").first
        point_and_maybe_click(self.page, ta, "先打一句中文", click=True, hold_ms=800)
        self.page.keyboard.type(text, delay=45)
        self.page.wait_for_timeout(400)
        self.mark("voice_type", "先打一句中文", t0=t0, zoom="zoom_panel", text=text)
        t0 = _now(self.page)
        gen = self.page.locator("#ws-cp-voice button.gen-btn").first
        point_and_maybe_click(self.page, gen, caption or "点【生成语音】用克隆声念", click=True, hold_ms=1000)
        js_audio = "() => { const el=document.getElementById('ws-cp-voice'); const r=el&&(el.shadowRoot||el); const a=r&&r.querySelector('audio'); return a ? (a.getAttribute('src')||a.currentSrc||'') : ''; }"
        ok = _wait_for(self.page, f"() => !!({js_audio})()", 75000, 500)
        src = self.page.evaluate(js_audio)
        audio_at = _now(self.page)
        print(f"    voice preview ok={ok} src={str(src)[:80]}")
        audio_file = None
        if src:
            self.voice_n += 1
            audio_file = _fetch_audio(src, self.epdir / f"voice_{self.clip}_{self.voice_n}")
        au = self.page.locator("#ws-cp-voice audio").first
        point_and_maybe_click(self.page, au if au.count() else None, "所听即所发：是她自己的声音", click=False, hold_ms=2400)
        self.mark("voice_gen", caption, t0=t0, zoom="zoom_panel", text=text, ok=ok,
                  audio=(str(audio_file.relative_to(ROOT)) if audio_file else ""),
                  audio_at=(audio_at if audio_file else None))
        sent = False
        if send and src:
            t0 = _now(self.page)
            before_out = _count(self.page, ".msg-row.out")
            sb = self.page.locator("#ws-cp-voice button").filter(has_text="发送").first
            if sb.count():
                point_and_maybe_click(self.page, sb, "发给客人", click=True, hold_ms=1000)
                sent = _wait_for(self.page, "() => document.querySelectorAll('.msg-row.out').length > %d" % before_out, 40000)
                self.page.wait_for_timeout(1500)
                loc = self.page.locator(".msg-row.out .msg-bubble")
                el = loc.nth(loc.count() - 1) if loc.count() else None
                point_and_maybe_click(self.page, el, "语音消息已发出", click=False, hold_ms=1600)
            print(f"    voice sent={sent}")
            self.mark("voice_send", "发给客人", t0=t0, zoom="zoom", sent=sent)

    def mode_switch(self, value: str, caption: str = "", zoom: str = "zoom") -> None:
        """会话 AI 档位（唯一入口 #mode-select）：manual / ai_draft / ai_multi / auto_ai。"""
        t0 = _now(self.page)
        if value == "auto_ai" and self.st.get("chat_type") == "group":
            # 群里全自动 = AI 在群里自动说话，产品要求显式确认；教学片只讲私聊，群舞台直接拒绝
            print("    ✗ 群舞台拒绝切全自动（剧本只对私聊升档）")
            self.mark("mode_switch", caption, t0=t0, zoom=zoom, value=value, ok=False, refused="group")
            return
        # #mode-select 是被自定义 UI 包住的原生 select（select_option 点不到）→ 走它自己的 change 通道
        anchor = _first_css(self.page, "#chat-header, .chat-header") or _first_css(self.page, "#mode-select")
        point_and_maybe_click(self.page, anchor, caption or f"档位切到【{MODES.get(value, value)}】", click=False, hold_ms=1500)
        ok = False
        try:
            self.page.select_option("#mode-select", value, timeout=2500)
            ok = True
        except Exception:
            ok = bool(self.page.evaluate(
                """(v) => { const s = document.getElementById('mode-select'); if (!s) return false;
                     s.value = v; s.dispatchEvent(new Event('change', {bubbles: true}));
                     return s.value === v; }""", value))
        # D-M9 手动粘性三选：切到手动时工作台先弹「切到手动后，AI 什么时候接回？」（一直手动 / 30 分钟后接回 /
        # 离开会话时接回 / 取消）。不答它：下拉回滚、档位没切，后面 type_send 是在弹窗底下打字，成片里弹窗盖着
        # 会话（2026-09-18 P8 重录 t67 实锤：头栏仍「半自动」）。按剧本「AI 让位」选缺省「一直手动」。
        confirmed = None
        if value == "manual":
            okbtn = None
            for _ in range(10):
                self.page.wait_for_timeout(250)
                okbtn = _first_css(self.page, '.app-confirm-mask .app-confirm-card [data-act="ok"]')
                if okbtn is not None:
                    break
            if okbtn is not None:
                point_and_maybe_click(self.page, okbtn, "一直手动，AI 不再插话", click=True, hold_ms=1200)
                confirmed = True
                for _ in range(12):
                    self.page.wait_for_timeout(250)
                    if _first_css(self.page, ".app-confirm-mask") is None:
                        break
            else:
                confirmed = False   # 没弹（已是手动 / 版本不同）——不算错，记下来
        self.page.wait_for_timeout(2200)
        ok = ok and bool(self.page.evaluate("(v) => { const s=document.getElementById('mode-select'); return !!s && s.value===v; }", value))
        ok = ok and _first_css(self.page, ".app-confirm-mask") is None
        print(f"    mode_switch {value} ok={ok} confirmed={confirmed}")
        point_and_maybe_click(self.page, anchor, "AI 值守，客户不用等" if value != "manual" else "AI 让位，这条自己聊",
                              click=False, hold_ms=1700)
        self.mark("mode_switch", caption, t0=t0, zoom=zoom, value=value, ok=ok, confirmed=confirmed)

    def _rows_visible(self) -> int:
        try:
            return int(self.page.evaluate(
                "() => [...document.querySelectorAll('#conv-items .conv-item')]"
                ".filter(e => e.offsetWidth || e.offsetHeight).length"))
        except Exception:
            return -1

    def urgent_filter(self, caption: str = "", zoom: str = "zoom_list") -> None:
        """筛「超时」→「需人工」→ 回「全部」。两个筛选下各数一次可见行，记进 marker：
        字幕说「先举手」而列表是「没有符合当前筛选的对话」（2026-09-17/18 P3 两次成片实锤）
        是成片级缺陷，gate 对 sla_rows==0 记 WARN；「需人工」页签 0 条时是隐藏的，点不到就不点。"""
        t0 = _now(self.page)
        sla = _first_css(self.page, '.ftab[data-f="sla"]')
        point_and_maybe_click(self.page, sla, caption or "筛【超时】", click=True, hold_ms=1300)
        self.page.wait_for_timeout(1400)
        sla_rows = self._rows_visible()
        attn_rows = -1
        attn = _first_css(self.page, '.ftab[data-f="attn"]')
        attn_visible = False
        if attn is not None:
            try:
                attn_visible = bool(attn.is_visible())
            except Exception:
                attn_visible = False
        if attn_visible:
            point_and_maybe_click(self.page, attn, "【需人工】先举手", click=True, hold_ms=1300)
            self.page.wait_for_timeout(1400)
            attn_rows = self._rows_visible()
        else:
            print("    ! WARN 「需人工」页签隐藏（0 条 crit/需人工会话）——字幕会压空；查 sla_demo 是否铺下去")
        lst = _first_css(self.page, ".conv-list, #conv-list, .conv-item")
        point_and_maybe_click(self.page, lst, "最急的排最前", click=False, hold_ms=1500)
        allf = _first_css(self.page, '.ftab[data-f="all"]')
        if allf is not None:
            try:
                allf.click(timeout=2000)
            except Exception:
                pass
        print(f"    urgent_filter sla_rows={sla_rows} attn_rows={attn_rows}")
        self.mark("urgent_filter", caption, t0=t0, zoom=zoom, sla_rows=sla_rows, attn_rows=attn_rows)

    # ── 分发 ────────────────────────────────────────────────────────────
    def run(self, steps: list) -> None:
        inject_pointer(self.page)
        hide_staging_artifacts(self.page)
        for st in steps:
            op, *args = st
            try:
                if op == "open_stage":
                    self.open_stage(*args)
                elif op == "narr":
                    self.narr(*args)
                elif op == "incoming":
                    self.incoming(*args)
                elif op == "xlate_quick_on":
                    self.xlate_quick_on(*args)
                elif op == "ai_reply_send":
                    self.ai_reply_send(*args)
                elif op == "type_send":
                    self.type_send(*args)
                elif op == "voice_preview":
                    self.voice_preview(*args)
                elif op == "urgent_filter":
                    self.urgent_filter(*args)
                elif op == "mode_switch":
                    self.mode_switch(*args)
                elif op == "use_stage":
                    self.use_stage(*args)
                elif op == "ensure_mode":
                    self.ensure_mode(*args)
                elif op == "list_filter":
                    self.list_filter(*args)
                elif op == "kb_answer":
                    self.kb_answer(*args)
                elif op == "conv_scope":
                    self.conv_scope(*args)
                elif op == "say":
                    self.say(*args)
                elif op == "wait_outbound":
                    self.wait_outbound(*args)
                elif op == "click_takeover":
                    self.click_takeover(*args)
                elif op == "assert_quiet":
                    self.assert_quiet(*args)
                elif op == "ai_diag":
                    self.ai_diag(*args)
                elif op == "goto_page":
                    self.goto_page(*args)
                elif op == "cg_step":
                    self.cg_step(*args)
                elif op == "wx_env":
                    self.wx_env(*args)
                elif op == "wx_tier":
                    self.wx_tier(*args)
                elif op == "wx_verify":
                    self.wx_verify(*args)
                elif op == "wx_test_inbound":
                    self.wx_test_inbound(*args)
                elif op == "wx_restore":
                    self.wx_restore()
                elif op == "wait_draft":
                    self.wait_draft(*args)
                elif op == "adopt_draft":
                    self.adopt_draft(*args)
                else:
                    # 沿用 record_chatx 的静态原语（goto / point_css / expand_card / cp_tab …）；末位可选景别
                    t0 = _now(self.page)
                    zoom = ""
                    if args and isinstance(args[-1], str) and args[-1] in ZOOM_WORDS:
                        zoom = args[-1]
                        args = args[:-1]
                    run_steps(self.page, [[op, *args]], allow_send=False)
                    if op == "goto":
                        wait_chat_ready(self.page, 12000)
                        hide_staging_artifacts(self.page)   # 整页重载后样式丢失，补注
                        if self.content_offset is None:
                            self.content_offset = _now(self.page)
                    self.mark(op, args[1] if len(args) > 1 and isinstance(args[1], str) else "", t0=t0, zoom=zoom)
            except ClipAbort:
                raise
            except Exception as e:  # noqa: BLE001
                if op in ("incoming", "wait_outbound", "wx_env", "wx_tier", "wx_verify", "wx_test_inbound", "wait_draft"):
                    raise ClipAbort(f"{op} 异常: {str(e)[:160]}") from e
                print(f"    ~ 步骤 {st} 软失败: {str(e)[:160]}")
                self.mark(op, "", t0=_now(self.page), error=str(e)[:120])


def restore_wx_policy() -> dict:
    """副驾档位还原（幂等）：有快照文件才动；还原成功删快照。wx_restore 步骤 / _cleanup / 下次开录都调它——
    任一处崩掉，下一处兜底；快照文件不存在 = 没动过或已还原。"""
    if not WX_SNAPSHOT_FILE.exists():
        return {"ok": True, "skipped": "no_snapshot"}
    try:
        snap = json.loads(WX_SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"bad snapshot: {str(e)[:80]}"}
    r = wx_policy_restore(snap)
    if r.get("ok"):
        WX_SNAPSHOT_FILE.unlink(missing_ok=True)
    return {**r, "snapshot": snap}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--only", help="只录这些段，逗号分隔（如 d_arm,e_live）")
    ap.add_argument("--keep-kb", action="store_true", help="录完保留演示知识条目（重录省一步；默认录完即删）")
    ap.add_argument("--keep-persona", action="store_true", help="录完保留演示人设及会话绑定（默认录完解绑并删）")
    a = ap.parse_args()
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    ep = next(e for e in cur["episodes"] if e["id"] == a.episode)
    sc_ep = dict(next((e for e in SC["episodes"] if e["id"] == a.episode), {}))
    for key in ("persona_demo", "sla_demo", "kb_demo_entry"):
        sc_ep[key] = resolve_demo_ref(sc_ep.get(key), key)
    st = stage(ep["stage"])
    stage_names = sc_ep.get("stages") or [ep["stage"]]
    out = ROOT / "out" / ep["id"]
    raw = out / "raw"
    kb_id = ensure_kb_entry(sc_ep.get("kb_demo_entry")) if sc_ep.get("kb_demo_entry") else None
    demo_pid = ensure_demo_persona(sc_ep["persona_demo"]) if sc_ep.get("persona_demo") else None
    sla_spec = sc_ep.get("sla_demo")
    sla_st = stage(sla_spec["stage"]) if sla_spec else None
    sla_seeded: list[dict] = []
    out_lang = str(sc_ep.get("out_lang") or "").strip()
    if not out_lang:
        _src = str(sc_ep.get("lang_pair") or "").split("→")[0].strip()
        out_lang = _src if _src and "/" not in _src else ""

    def _cleanup() -> None:
        if kb_id and not a.keep_kb:
            print("  删除演示知识条目 →", delete_kb_entry(kb_id))
        if demo_pid and not a.keep_persona:
            print("  解绑并删除演示人设 →", remove_demo_persona(sc_ep["persona_demo"]))
        if sla_seeded:
            print("  删 SLA 演示会话 →", drop_sla_demo(sla_st, sla_seeded))
        if sc_ep.get("format") == "tutorial":
            # 教学片把舞台会话切过全自动 / 接管：录完交还接管、档位归 review，共享实例上不留一条真会自动发的会话
            for name in stage_names:
                s = stage(name)
                if s.get("chat_type") == "group":
                    continue
                if s.get("ephemeral"):
                    # 演示客户（F2 小林）整条硬删：老板收件箱不留假客户；settings/drafts 随会话一起删
                    print(f"  删演示会话 {name} →", delete_stage_conversation(s))
                    continue
                print(f"  交还接管 {name} →", end_takeover(s))
                print(f"  档位归位 {name} →", set_automation_mode(s, "review"))
        # F2 引导页真点过「保存」→ 副驾档位还原（wx_restore 步骤已还原则这里是 no-op）
        r = restore_wx_policy()
        if r.get("skipped") != "no_snapshot":
            print("  副驾档位还原 →", r)

    ok = 0
    # 上次录制若在 wx_tier 之后崩掉，快照文件还在：开录前先还原老板的档位，再从干净状态开始
    stale = restore_wx_policy()
    if stale.get("skipped") != "no_snapshot":
        print("  [上次残留] 副驾档位还原 →", stale)
    try:
        if sla_spec:
            # 概览段的「超时 / 需人工」要有真行可筛：铺演示来信（backfill 入库，不起草不发），录完硬删
            sla_seeded = seed_sla_demo(sla_st, sla_spec)
            print("  铺 SLA 演示会话 →", sla_seeded)
            vis = sla_demo_visible(sla_st, sla_seeded)
            print("  SLA 演示会话服务端口径 →", vis)
            if not vis.get("ok"):
                raise SystemExit(f"[FAIL] SLA 演示会话未被算成超时（{vis}），字幕会压空列表，不录")
            # 等引擎把 backfill 当「同步完成」结算掉，再把沉寂清单项/登录确认框收掉——否则弹窗盖画面
            settled = settle_sla_demo(sla_st, sla_seeded)
            print("  SLA 演示副作用收口 →", settled)
            if settled.get("pending_left") or settled.get("login_pending_left"):
                raise SystemExit(f"[FAIL] 沉寂清单/登录确认框未收干净（{settled}），录了会弹窗盖画面，不录")
        ok = _record_clips(a, ep, sc_ep, st, stage_names, out, raw, out_lang)
    finally:
        # 任何异常（Playwright 超时 / 引擎 5xx）都得把演示人设、知识条目、SLA 演示行收干净——共享实例不留垃圾
        _cleanup()
    if ok < 0:
        return 2
    print(f"DONE {ok} clips")
    return 0


def _record_clips(a, ep: dict, sc_ep: dict, st: dict, stage_names: list, out: Path, raw: Path, out_lang: str) -> int:
    ok = 0
    only = {s.strip() for s in (a.only or "").split(",") if s.strip()}
    for clip in ep["footage_actions"]:
        if only and clip["clip"] not in only:
            continue
        print(f"== {ep['id']} {clip['clip']}: {clip['desc']}")
        if clip.get("clear_stage"):
            for name in stage_names:
                if stage(name).get("ephemeral"):
                    # 演示会话：上次录的整条删掉（含旧草稿 / 档位行），比 clear 干净；下面再钉 review
                    print(f"  删旧演示会话 {name} →", delete_stage_conversation(stage(name)))
                else:
                    print(f"  清空舞台 {name} →", clear_seller_view(stage(name)))
                # 上一集 / 上一次教学片可能把会话留在「接管中」（takeover 表 + manual）：先交还，档位才切得动
                print(f"  交还接管 {name} →", end_takeover(stage(name)))
                # 顶栏档位归位：上一集留下的「人工模式 / AI 已让位」横幅不许带进这一集
                print(f"  档位归位 {name} →", set_automation_mode(stage(name), "review"))
                # 出站语言钉成剧本客户语言（lang_pair 源语；多语种「ar/en」不猜，除非集里显式 out_lang）
                if out_lang:
                    print(f"  出站语言钉 {name} → {out_lang}", set_outbound_lang(stage(name), out_lang))
        with session(raw) as (page, ctx):
            # 工作台的 window.confirm（如「目标语≠客户语言」阻断确认）：Playwright 默认 dismiss=取消 → 发不出去
            page.on("dialog", lambda d: d.accept())
            page.wait_for_timeout(1500)
            try:
                el = page.get_by_text("跳过引导", exact=False).first
                if el.count() and el.is_visible():
                    el.click(timeout=2000)
                    page.wait_for_timeout(500)
            except Exception:
                pass
            inject_pointer(page)
            ready_offset = _now(page)
            rec = Recorder(page, st, out, clip["clip"])
            try:
                rec.run(clip["steps"])
            except ClipAbort as e:
                print(f"  [ABORT] {ep['id']} {clip['clip']}: {e}")
                return -1
            asserts = dom_assert(page)
            page.wait_for_timeout(1200)
            end_t = _now(page)
        tgt = latest_video(raw, out / f"footage_{clip['clip']}.webm")
        d = dur(tgt)
        meta = {
            "clip": clip["clip"], "desc": clip["desc"], "duration": d, "ready_offset": round(ready_offset, 2),
            "content_offset": round(rec.content_offset or ready_offset, 2), "end_t": end_t,
            "stage": ep["stage"], "stages": stage_names, "markers": rec.markers, "asserts": asserts,
        }
        tgt.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  [OK] {tgt.name} {d:.1f}s markers={len(rec.markers)} asserts={asserts}")
        ok += 1
    return ok


def _api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def resolve_demo_ref(spec: dict | None, key: str) -> dict | None:
    """演示块可写 {"ref": "P1"} 复用另一集同名块（F1 复用 P1 人设 / P3 SLA 行），避免几十行 JSON 抄两份漂移。
    非 ref 原样返回；ref 指向不存在或那集没有该块 → SystemExit（配置错，不录）。"""
    if not isinstance(spec, dict) or not spec.get("ref"):
        return spec
    src = next((e for e in SC["episodes"] if e["id"] == spec["ref"]), None)
    if not src or not isinstance(src.get(key), dict):
        raise SystemExit(f"[FAIL] {key} ref={spec['ref']} 不存在或那集无 {key}")
    got = dict(src[key])
    got["_ref"] = spec["ref"]
    return got


def ensure_kb_entry(spec: dict) -> str:
    """P7 演示知识条目：按 title 找，没有就建（source=user，桌面模式下可被斜杠面板检索到；vendor 条目检索面硬排除）。"""
    found = [e for e in _api("GET", "/api/kb/entries?search=" + urllib.parse.quote(spec["title"])).get("entries", [])
             if e.get("title") == spec["title"]]
    if found:
        print(f"  知识条目已在：{spec['title']} ({found[0]['id']})")
        return found[0]["id"]
    body = {k: v for k, v in spec.items() if not k.startswith("_")}
    body.update({"enabled": True, "source": "user", "_force_save_triggers": True, "_force_save": True})
    d = _api("POST", "/api/kb/entries", body)
    if not d.get("ok"):
        raise SystemExit(f"[FAIL] 建演示知识条目失败：{str(d)[:200]}")
    print(f"  已建演示知识条目：{spec['title']} ({d['id']})")
    return d["id"]


def delete_kb_entry(entry_id: str) -> dict:
    try:
        return _api("DELETE", f"/api/kb/entries/{entry_id}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}


def _conv_ref(st: dict) -> dict:
    """卖家视角会话的三元组：AI 拟稿读的是这条会话的生效人设。"""
    return {"platform": st["platform"], "account_id": st["seller_account"], "chat_key": st["customer_account"]}


def ensure_demo_persona(spec: dict) -> str:
    """P2 演示人设：舞台卖家账号自带人设与剧本身份不符（烧烤店老板娘 / 智聊支持）→ AI 对 B2B 询价会答非所问。
    导入 profile（merge，已存在即覆盖为剧本版本）→ 会话级绑定到各舞台卖家视角会话 → 复核 effective 生效。"""
    prof = spec["profile"]
    pid = prof["id"]
    d = _api("POST", "/api/personas/profiles/import", {"profiles": [prof], "mode": "merge"})
    if not d.get("ok") or d.get("invalid"):
        raise SystemExit(f"[FAIL] 导入演示人设失败：{str(d)[:200]}")
    print(f"  演示人设 {pid} 已导入（add={d.get('add')} overwrite={d.get('overwrite')}）")
    for name in spec.get("bind_stages") or []:
        st = stage(name)
        ref = _conv_ref(st)
        b = _api("POST", "/api/persona/bind", {"scope": "conversation", "profile_id": pid, **ref})
        eff = _api("GET", "/api/persona/effective?" + urllib.parse.urlencode(ref)).get("effective") or {}
        print(f"    bind {name} {b.get('conversation_key')} {b.get('from') or '-'}→{b.get('to')} effective={eff.get('id')}")
        if eff.get("id") != pid:
            raise SystemExit(f"[FAIL] {name} 会话生效人设仍是 {eff.get('id')}，AI 拟稿会跑偏，不录")
    return pid


def remove_demo_persona(spec: dict) -> dict:
    pid = spec["profile"]["id"]
    res: dict = {"unbind": {}, "delete": None}
    for name in spec.get("bind_stages") or []:
        try:
            res["unbind"][name] = _api("POST", "/api/persona/unbind", {"scope": "conversation", **_conv_ref(stage(name))}).get("ok")
        except Exception as e:  # noqa: BLE001
            res["unbind"][name] = str(e)[:80]
    try:
        res["delete"] = _api("DELETE", f"/api/personas/profiles/{pid}").get("ok")
    except Exception as e:  # noqa: BLE001
        res["delete"] = str(e)[:80]
    return res


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(main())
