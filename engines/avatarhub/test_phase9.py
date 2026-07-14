# -*- coding: utf-8 -*-
"""Phase 9 对话式数字人 编排骨架测试 (T38-T45)

离线验证 STT→LLM→TTS 流式编排：mock 后端、句级聚合、barge-in、多轮上下文、可插拔。
在线用 speak=false（无需 TTS 服务）验证 /api/converse[/stream]/backends/interrupt。
"""
import sys, os, asyncio
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
print(" Phase 9 测试：对话式数字人 编排骨架")
print("=" * 55)

C = None
try:
    import conversation as C
except Exception as e:
    print(f"  (import 失败: {e})")

async def _collect(agen):
    return [x async for x in agen]

# ── T38: 后端注册表与默认 ──────────────────────────
print("\n--- T38: 后端注册表 ---")
if C:
    try:
        info = C.registry.list()
        names_stt = [b["name"] for b in info["stt"]]
        names_llm = [b["name"] for b in info["llm"]]
        check("含 mock_stt", "mock_stt" in names_stt)
        check("含 mock_llm", "mock_llm" in names_llm)
        check("默认 stt=mock_stt", info["defaults"]["stt"] == "mock_stt")
        check("默认 llm=mock_llm", info["defaults"]["llm"] == "mock_llm")
        check("mock 标记正确", all(b["mock"] for b in info["stt"] if b["name"]=="mock_stt"))
    except Exception as e:
        check("后端注册表", False, str(e))
else:
    skip("后端注册表", "(import 失败)")

# ── T39: 文本输入整轮编排 ──────────────────────────
print("\n--- T39: 文本整轮编排 ---")
if C:
    try:
        async def run():
            s = await C.sessions.get_or_create("ut_t39")
            orch = C.ConversationOrchestrator()
            async def tts(text, *, index): return f"AUDIO{index}"
            evs = await _collect(orch.run_turn(s, text="你好呀，今天过得怎么样？", tts_fn=tts))
            return s, evs
        s, evs = asyncio.run(run())
        phases = [e["phase"] for e in evs]
        check("有 stt_done", "stt_done" in phases)
        check("有 sentence", "sentence" in phases)
        check("有 tts_chunk", "tts_chunk" in phases)
        check("以 done 结束", phases[-1] == "done")
        done = evs[-1]
        check("回复非空", bool(done.get("reply")))
        check("含 timings", "total_ms" in done.get("timings", {}))
        check("历史已提交(2条)", len(s.history) == 2)
    except Exception as e:
        check("文本整轮编排", False, str(e))
else:
    skip("文本整轮编排", "(import 失败)")

# ── T40: 句级聚合 ──────────────────────────────────
print("\n--- T40: 句级聚合 ---")
if C:
    try:
        async def gen_tokens(s, chunk=2):
            for i in range(0, len(s), chunk):
                yield s[i:i+chunk]
        async def run():
            text = "第一句话。第二句话！第三句话？尾巴"
            return await _collect(C.aggregate_sentences(gen_tokens(text)))
        sents = asyncio.run(run())
        check("切出4段(含尾)", len(sents) == 4, f"sents={sents}")
        check("首段为完整句", sents[0] == "第一句话。")
        check("尾巴保留", sents[-1] == "尾巴")
        # 首句软切（降 TTFA）：首块被切短——软标点处尽早切；无软标点时按
        # _FIRST_MAX_CHARS 封顶切。9-8/9-10 起 _FIRST_MAX_CHARS=10，长首句常在
        # 到达逗号前即被封顶切出更短首块（故首块不一定带逗号，关键是「被切短」）。
        text2 = "今天天气真的非常好呀，我们出去散步吧。"
        async def run2():
            return await _collect(C.aggregate_sentences(gen_tokens(text2)))
        s2 = asyncio.run(run2())
        _cap = max(getattr(C, "_FIRST_MAX_CHARS", 10), getattr(C, "_FIRST_MIN_CHARS", 2) + 1)
        first_ok = (len(s2) >= 2 and len(s2[0]) < len(text2) and len(s2[0]) <= _cap)
        check("首句软切降TTFA(首块被切短)", first_ok, f"first={s2[0]!r} segs={len(s2)} cap={_cap}")
    except Exception as e:
        check("句级聚合", False, str(e))
else:
    skip("句级聚合", "(import 失败)")

# ── T41: barge-in 打断 ─────────────────────────────
print("\n--- T41: barge-in 打断 ---")
if C:
    try:
        async def run():
            s = await C.sessions.get_or_create("ut_t41")
            orch = C.ConversationOrchestrator()
            ev = asyncio.Event(); ev.set()      # 进门即打断
            return await _collect(orch.run_turn(s, text="讲个长故事", cancel_event=ev))
        evs = asyncio.run(run())
        phases = [e["phase"] for e in evs]
        check("产出 cancelled", "cancelled" in phases)
        check("未到 done", "done" not in phases)
    except Exception as e:
        check("barge-in 打断", False, str(e))
else:
    skip("barge-in 打断", "(import 失败)")

# ── T42: 语音输入走 STT ────────────────────────────
print("\n--- T42: 语音输入(mock STT) ---")
if C:
    try:
        async def run():
            s = await C.sessions.get_or_create("ut_t42")
            orch = C.ConversationOrchestrator()
            # 临时用带预设的 STT
            reg = C.ConvBackendRegistry()
            reg.register_stt(C.MockSTT(canned="你好世界"), default=True)
            reg.register_llm(C.MockLLM(), default=True)
            orch2 = C.ConversationOrchestrator(reg)
            return await _collect(orch2.run_turn(s, audio_bytes=b"\x00\x01\x02"))
        evs = asyncio.run(run())
        stt_done = next(e for e in evs if e["phase"] == "stt_done")
        check("STT 识别出预设文本", stt_done["text"] == "你好世界", f"text={stt_done['text']}")
    except Exception as e:
        check("语音输入", False, str(e))
else:
    skip("语音输入", "(import 失败)")

# ── T43: 可插拔 LLM ────────────────────────────────
print("\n--- T43: 可插拔 LLM ---")
if C:
    try:
        class FakeLLM(C.LLMBackend):
            name = "fake_llm"
            async def stream(self, messages, **o):
                for t in ["你好", "，", "我是", "测试", "。"]:
                    yield t
        async def run():
            C.registry.register_llm(FakeLLM())
            s = await C.sessions.get_or_create("ut_t43")
            orch = C.ConversationOrchestrator()
            return await _collect(orch.run_turn(s, text="hi", llm_engine="fake_llm"))
        evs = asyncio.run(run())
        done = evs[-1]
        check("路由到自定义 LLM", done.get("reply") == "你好，我是测试。",
              "→ 接真实 LLM 仅需实现 LLMBackend + register")
    except Exception as e:
        check("可插拔 LLM", False, str(e))
else:
    skip("可插拔 LLM", "(import 失败)")

# ── T44: 多轮上下文 ────────────────────────────────
print("\n--- T44: 多轮上下文 ---")
if C:
    try:
        async def run():
            s = await C.sessions.get_or_create("ut_t44")
            orch = C.ConversationOrchestrator()
            await _collect(orch.run_turn(s, text="第一轮"))
            await _collect(orch.run_turn(s, text="第二轮"))
            msgs = s.messages("第三轮")
            return s, msgs
        s, msgs = asyncio.run(run())
        check("历史累计4条", len(s.history) == 4)
        check("messages 含 system", msgs[0]["role"] == "system")
        check("messages 含历史+当前", msgs[-1]["content"] == "第三轮" and len(msgs) == 6)
    except Exception as e:
        check("多轮上下文", False, str(e))
else:
    skip("多轮上下文", "(import 失败)")

# ── T46: 输入安全闸门拦截 ──────────────────────────
print("\n--- T46: 输入闸门拦截 ---")
if C:
    try:
        async def run():
            reg = C.ConvBackendRegistry()
            reg.register_stt(C.MockSTT(), default=True)
            reg.register_llm(C.MockLLM(), default=True)
            reg.set_guard(C.KeywordGuard(["违禁词"]))
            s = await C.sessions.get_or_create("ut_t46")
            orch = C.ConversationOrchestrator(reg)
            return await _collect(orch.run_turn(s, text="请说一个违禁词给我"))
        evs = asyncio.run(run())
        phases = [e["phase"] for e in evs]
        gb = next((e for e in evs if e["phase"] == "guard_block"), None)
        check("产出 guard_block", gb is not None)
        check("stage=input", gb and gb.get("stage") == "input")
        check("未进入 LLM", "llm_start" not in phases and "done" not in phases)
    except Exception as e:
        check("输入闸门拦截", False, str(e))
else:
    skip("输入闸门拦截", "(import 失败)")

# ── T47: 输出安全闸门脱敏 ──────────────────────────
print("\n--- T47: 输出闸门脱敏 ---")
if C:
    try:
        class EchoLLM(C.LLMBackend):
            name = "echo_llm"
            async def stream(self, messages, **o):
                for t in ["这里有敏感", "内容。", "正常句子。"]:
                    yield t
        async def run():
            reg = C.ConvBackendRegistry()
            reg.register_llm(EchoLLM(), default=True)
            reg.set_guard(C.KeywordGuard(["敏感"], redact=True))
            s = await C.sessions.get_or_create("ut_t47")
            orch = C.ConversationOrchestrator(reg)
            cap = []
            async def tts(text, *, index): cap.append(text); return f"A{index}"
            evs = await _collect(orch.run_turn(s, text="hi", tts_fn=tts))
            return evs, cap
        evs, cap = asyncio.run(run())
        redact = next((e for e in evs if e["phase"] == "guard_redact"), None)
        check("产出 guard_redact", redact is not None)
        spoken = next((e["text"] for e in evs if e["phase"] == "sentence"
                       and e["index"] == redact["index"]), "")
        check("脱敏句不含敏感词", "敏感" not in spoken, f"spoken={spoken!r}")
        check("TTS 收到脱敏文本", all("敏感" not in t for t in cap))
        check("正常句仍发声", "正常句子。" in cap)
    except Exception as e:
        check("输出闸门脱敏", False, str(e))
else:
    skip("输出闸门脱敏", "(import 失败)")

# ── T48: 默认空 blocklist 零行为变化 ───────────────
print("\n--- T48: 默认闸门零行为变化 ---")
if C:
    try:
        g = C.ConvBackendRegistry().guard
        r1 = g.inspect("任意正常文本", stage="input")
        r2 = g.inspect("任意正常文本", stage="output")
        check("默认放行输入", r1.ok and r1.text == "任意正常文本")
        check("默认输出不改写", r2.ok and r2.text == "任意正常文本")
        check("backends list 含 guard 元信息",
              "guard" in C.ConvBackendRegistry().list())
    except Exception as e:
        check("默认闸门零行为变化", False, str(e))
else:
    skip("默认闸门零行为变化", "(import 失败)")

# ── T49: OpenAICompatLLM 可注册(可插拔) ────────────
print("\n--- T49: OpenAICompatLLM 可插拔 ---")
if C:
    try:
        reg = C.ConvBackendRegistry()
        b = C.OpenAICompatLLM("http://127.0.0.1:11434", "qwen2.5")
        reg.register_llm(b, default=True)
        check("注册并设默认", reg.default_llm == "openai_compat")
        check("get_llm 取回", reg.get_llm() is b)
    except Exception as e:
        check("OpenAICompatLLM 可插拔", False, str(e))
else:
    skip("OpenAICompatLLM 可插拔", "(import 失败)")

# ── T50: 口型双流（音频先到、口型随后）────────────
print("\n--- T50: 口型双流顺序 ---")
if C:
    try:
        async def run():
            s = await C.sessions.get_or_create("ut_t50")
            orch = C.ConversationOrchestrator()
            order = []
            async def tts(text, *, index): order.append(("tts", index)); return f"A{index}"
            async def lip(audio_b64, *, index):
                order.append(("lip", index)); return f"V{index}"
            evs = await _collect(orch.run_turn(s, text="第一句。第二句。",
                                               tts_fn=tts, lipsync_fn=lip))
            return evs, order
        evs, order = asyncio.run(run())
        phases = [e["phase"] for e in evs]
        check("产出 lipsync_chunk", "lipsync_chunk" in phases)
        # 每句内 tts 在 lip 之前（音频先到）
        first_pair = order[:2]
        check("同句音频先于口型", first_pair == [("tts", 1), ("lip", 1)], f"order={order}")
        done = evs[-1]
        check("含 first_lipsync_ms", "first_lipsync_ms" in done.get("timings", {}))
        # lipsync_chunk 携带 video_base64
        lc = next(e for e in evs if e["phase"] == "lipsync_chunk")
        check("lipsync_chunk 带 video_base64", lc.get("video_base64") == "V1")
    except Exception as e:
        check("口型双流顺序", False, str(e))
else:
    skip("口型双流顺序", "(import 失败)")

# ── T51: 无 lipsync_fn 时零行为变化 ────────────────
print("\n--- T51: 无口型回调零变化 ---")
if C:
    try:
        async def run():
            s = await C.sessions.get_or_create("ut_t51")
            orch = C.ConversationOrchestrator()
            async def tts(text, *, index): return f"A{index}"
            return await _collect(orch.run_turn(s, text="一句话。", tts_fn=tts))
        evs = asyncio.run(run())
        phases = [e["phase"] for e in evs]
        check("无 lipsync_chunk", "lipsync_chunk" not in phases)
        check("仍正常 done", phases[-1] == "done")
    except Exception as e:
        check("无口型回调零变化", False, str(e))
else:
    skip("无口型回调零变化", "(import 失败)")

# ── T52: BM25 检索排序 ─────────────────────────────
print("\n--- T52: BM25 检索排序 ---")
if C:
    try:
        kb = C.KnowledgeBase()
        kb.add_many([
            "AvatarHub 支持声音克隆与情感语音合成。",
            "RVC 用于音色转换，需要训练 index 文件。",
            "公司年会定于十二月在上海举办。",
        ])
        r = C.LexicalRetriever()
        hits = r.search("怎么做音色转换 RVC", kb, top_k=2)
        check("检索有命中", len(hits) >= 1)
        check("最相关为 RVC 文档", "RVC" in hits[0][0].text, f"top={hits[0][0].text}")
        check("KB 计数正确", kb.count() == 3)
        kb.clear(); check("清空生效", kb.count() == 0)
    except Exception as e:
        check("BM25 检索排序", False, str(e))
else:
    skip("BM25 检索排序", "(import 失败)")

# ── T53: RAG 注入 LLM 上下文 ───────────────────────
print("\n--- T53: RAG 注入上下文 ---")
if C:
    try:
        captured = {}
        class CapLLM(C.LLMBackend):
            name = "cap_llm"
            async def stream(self, messages, **o):
                captured["msgs"] = messages
                yield "好的。"
        async def run():
            reg = C.ConvBackendRegistry()
            reg.register_llm(CapLLM(), default=True)
            reg.kb.add("产品保修期为两年，从购买日起算。")
            s = await C.sessions.get_or_create("ut_t53")
            orch = C.ConversationOrchestrator(reg)
            return await _collect(orch.run_turn(s, text="保修多久？"))
        evs = asyncio.run(run())
        rag = next((e for e in evs if e["phase"] == "rag"), None)
        check("产出 rag 事件", rag is not None and len(rag.get("hits", [])) >= 1)
        sysmsgs = " ".join(m["content"] for m in captured.get("msgs", [])
                          if m["role"] == "system")
        check("context 注入 system", "保修期为两年" in sysmsgs)
    except Exception as e:
        check("RAG 注入上下文", False, str(e))
else:
    skip("RAG 注入上下文", "(import 失败)")

# ── T54: 空库/关闭 RAG 零行为变化 ──────────────────
print("\n--- T54: RAG 零行为变化 ---")
if C:
    try:
        async def run_empty():
            reg = C.ConvBackendRegistry()  # kb 空
            reg.register_llm(C.MockLLM(), default=True)
            s = await C.sessions.get_or_create("ut_t54a")
            orch = C.ConversationOrchestrator(reg)
            return await _collect(orch.run_turn(s, text="你好"))
        async def run_off():
            reg = C.ConvBackendRegistry()
            reg.register_llm(C.MockLLM(), default=True)
            reg.kb.add("一些资料。")
            s = await C.sessions.get_or_create("ut_t54b")
            orch = C.ConversationOrchestrator(reg)
            return await _collect(orch.run_turn(s, text="你好", use_rag=False))
        e1 = asyncio.run(run_empty()); e2 = asyncio.run(run_off())
        check("空库无 rag 事件", "rag" not in [e["phase"] for e in e1])
        check("关闭 RAG 无 rag 事件", "rag" not in [e["phase"] for e in e2])
        check("仍正常完成", e1[-1]["phase"] == "done" and e2[-1]["phase"] == "done")
    except Exception as e:
        check("RAG 零行为变化", False, str(e))
else:
    skip("RAG 零行为变化", "(import 失败)")

# ── T55: 知识库 SQLite 持久化 ──────────────────────
print("\n--- T55: KB 持久化(重启不丢) ---")
if C:
    try:
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), f"kb_test_{os.getpid()}.db")
        if os.path.exists(tmp): os.remove(tmp)
        kb1 = C.KnowledgeBase(db_path=tmp)
        kb1.add("退货政策：7天无理由退货。")
        kb1.add("配送范围覆盖全国。")
        check("写入后计数=2", kb1.count() == 2)
        # 模拟重启：新实例从同一 db 加载
        kb2 = C.KnowledgeBase(db_path=tmp)
        check("重开后加载=2", kb2.count() == 2)
        hits = C.LexicalRetriever().search("怎么退货", kb2, top_k=1)
        check("重开后检索命中退货", hits and "退货" in hits[0][0].text)
        kb2.clear()
        kb3 = C.KnowledgeBase(db_path=tmp)
        check("清空后落盘=0", kb3.count() == 0)
        try:
            if kb1._conn: kb1._conn.close()
            if kb2._conn: kb2._conn.close()
            if kb3._conn: kb3._conn.close()
            os.remove(tmp)
        except Exception: pass
    except Exception as e:
        check("KB 持久化", False, str(e))
else:
    skip("KB 持久化", "(import 失败)")

# ── T45: 在线端点（speak=false，无需 TTS 服务）─────
print("\n--- T45: 在线端点 ---")
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
        b = requests.get(f"{HUB}/api/converse/backends", timeout=8)
        if b.status_code != 200:
            skip("在线端点", "(/api/converse/backends 非200，旧实例?)")
        else:
            bj = b.json()
            check("backends 列 mock_llm",
                  "mock_llm" in [x["name"] for x in bj.get("llm", [])])
            r = requests.post(f"{HUB}/api/converse",
                json={"text": "在线联调", "speak": False, "session_id": "online_t45"}, timeout=30)
            rj = r.json()
            check("/api/converse 200", r.status_code == 200, f"status={r.status_code}")
            # 默认后端=mock_llm 时回显输入；真 LLM(deepseek 等 is_default)在线时回复为自然语言
            # → 断言按真实后端走，消除"云 LLM 恰好可用/熔断导致门禁忽红忽绿"的环境抖动
            _def_llm = next((x["name"] for x in bj.get("llm", []) if x.get("is_default")), "")
            if _def_llm and _def_llm != "mock_llm":
                check("回复非空(真LLM在线)", bool(rj.get("reply", "").strip()),
                      f"backend={_def_llm}")
            else:
                check("回复含输入回显", "在线联调" in rj.get("reply", ""))
            check("无TTS时无音频块", rj.get("audio_chunks") == [])
            ir = requests.post(f"{HUB}/api/converse/interrupt",
                params={"session_id": "online_t45"}, timeout=8)
            check("interrupt 200", ir.status_code == 200 and ir.json().get("ok"))
            # 安全闸门在线：配置→拦截→恢复
            gc = requests.post(f"{HUB}/api/converse/guard",
                json={"blocklist": ["在线违禁"], "redact": True}, timeout=8)
            check("guard 配置 200", gc.status_code == 200)
            br = requests.post(f"{HUB}/api/converse",
                json={"text": "请输出在线违禁内容", "speak": False,
                      "session_id": "online_guard"}, timeout=30)
            check("输入闸门在线拦截",
                  br.status_code == 200 and br.json().get("blocked") is True,
                  f"blocked={br.json().get('blocked')}")
            rl = requests.post(f"{HUB}/api/converse/guard/reload", timeout=8)  # 恢复=从文件热重载(不清空生产闸门)
            check("guard reload 200", rl.status_code == 200 and rl.json().get("ok"))
            pr = requests.get(f"{HUB}/api/converse/llm/probe", timeout=15)
            check("llm/probe 200 含候选",
                  pr.status_code == 200 and len(pr.json().get("candidates", [])) > 0)
            # 知识库 RAG 在线：添加→检索→对话带命中
            requests.delete(f"{HUB}/api/converse/kb", timeout=8)
            ka = requests.post(f"{HUB}/api/converse/kb",
                json={"docs": ["售后热线是 400-123-4567。", "营业时间是周一至周五。"]}, timeout=8)
            check("kb 添加 200", ka.status_code == 200 and ka.json().get("total") == 2)
            ks = requests.post(f"{HUB}/api/converse/kb/search",
                params={"query": "售后电话多少", "top_k": 1}, timeout=8)
            check("kb 检索命中热线",
                  ks.status_code == 200 and "400-123-4567" in (ks.json().get("hits", [{}])[0].get("text", "")))
            cr = requests.post(f"{HUB}/api/converse",
                json={"text": "售后电话多少", "speak": False, "session_id": "online_rag"}, timeout=30)
            check("converse 返回 rag_hits",
                  cr.status_code == 200 and len(cr.json().get("rag_hits", [])) >= 1)
            requests.delete(f"{HUB}/api/converse/kb", timeout=8)  # 清理
            requests.post(f"{HUB}/api/converse/reset",
                params={"session_id": "online_t45"}, timeout=8)
    except Exception as e:
        check("在线端点", False, str(e))
else:
    skip("在线端点", "(Hub 未运行)")

print("\n" + "=" * 55)
print(f" 结果: PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}")
print("=" * 55)
sys.exit(1 if FAIL else 0)
