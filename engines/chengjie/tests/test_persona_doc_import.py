# -*- coding: utf-8 -*-
"""「从文档创建人设」LLM 结构化抽取管线（B 线）门禁——全部离线，fake chat_fn。

覆盖四层：
1. 纯函数 —— ``extract_docx_text``（zipfile 最小 docx + 强制 fallback）、
   ``_parse_llm_json`` 容错、``clamp_persona`` 预算护栏、``suggested_profile_id``。
2. 编排 —— ``run_extraction``（Call1+Call2 合并/identity 默认注入/Call3 溯源/
   coverage/completeness/坏 JSON 重试/两次都坏抛异常）。
2b. 抖动韧性 —— ``_call_llm_json`` 分类重试状态机（空响应原样重试 + 退避，坏 JSON
   附提示重试）与 ``_repair_truncated_json`` 截断修复（字符串内括号不误判）。
3. 任务注册表 —— create→running→done 生命周期、error 路径、并发上限拒绝、未知 id。
4. 路由 —— flag 开关 / parse（JSON+multipart）/ extract→jobs 轮询 / 404 / 429
   （TestClient 全链，chat_fn 经 ``app.state.persona_doc_chat_fn`` 注入假函数）。
5. 配置 —— 两个生产 yaml 可 safe_load 且新段形状正确。
"""

import asyncio
import copy
import json
import re
import sys
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils import persona_doc_import as pdi

_ENGINE_ROOT = Path(__file__).parent.parent


# ── 工具：现造最小 docx ───────────────────────────────────────────────────────

def _min_docx_bytes(paragraphs) -> bytes:
    """zipfile 现造最小 docx：只有 word/document.xml（两段中文即可测提取）。"""
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>"
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _clean_jobs():
    pdi.reset_jobs()
    yield
    pdi.reset_jobs()


# ── 1. extract_docx_text ─────────────────────────────────────────────────────

def test_extract_docx_text_minimal_zip():
    data = _min_docx_bytes(["她叫美月，32岁。", "在巴塞罗那做酒店运营。"])
    text = pdi.extract_docx_text(data)
    assert text == "她叫美月，32岁。\n在巴塞罗那做酒店运营。"


def test_extract_docx_text_fallback_when_docx_missing(monkeypatch):
    # sys.modules['docx']=None → import docx 抛 ImportError → 走 zipfile 兜底
    monkeypatch.setitem(sys.modules, "docx", None)
    data = _min_docx_bytes(["段落一", "段落二"])
    assert pdi.extract_docx_text(data) == "段落一\n段落二"


def test_extract_docx_text_entities_and_garbage():
    # HTML 实体 unescape；坏字节输入 → ""（软失败不抛）
    data = _min_docx_bytes(["A &amp; B &lt;C&gt;"])
    assert pdi.extract_docx_text(data) == "A & B <C>"
    assert pdi.extract_docx_text(b"not a zip at all") == ""


def test_extract_docx_text_real_python_docx():
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("第一段：身份卡。")
    doc.add_paragraph("第二段：工作经历。")
    buf = BytesIO()
    doc.save(buf)
    text = pdi.extract_docx_text(buf.getvalue())
    assert "第一段：身份卡。" in text and "第二段：工作经历。" in text


def test_prepare_text_and_count_paragraphs():
    text, truncated = pdi.prepare_text("a\r\n\n\n\nb  \r\nc\n")
    assert text == "a\n\nb\nc" and truncated is False
    assert pdi.count_paragraphs(text) == 3

    big = "x" * (pdi.MAX_TEXT_CHARS + 500)
    text2, truncated2 = pdi.prepare_text(big)
    assert truncated2 is True and len(text2) == pdi.MAX_TEXT_CHARS


# ── 2. _parse_llm_json ───────────────────────────────────────────────────────

def test_parse_llm_json_variants():
    assert pdi._parse_llm_json('{"a": 1}') == {"a": 1}
    assert pdi._parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert pdi._parse_llm_json(
        'Here is the JSON:\n{"a": {"b": 2}}\nHope this helps') == {"a": {"b": 2}}
    assert pdi._parse_llm_json('{"a": 1, "b": [1, 2,],}') == {"a": 1, "b": [1, 2]}
    assert pdi._parse_llm_json("totally not json") is None
    assert pdi._parse_llm_json("{broken") is None
    assert pdi._parse_llm_json("") is None
    assert pdi._parse_llm_json('["not", "a", "dict"]') is None


# ── 3. clamp_persona ─────────────────────────────────────────────────────────

def test_clamp_persona_budgets_and_types():
    persona = {
        "name": "  美月  ",
        "names": {"english": " Mizuki ", "nickname": "", "bogus": "x"},
        "role": "酒店运营总监助理",
        "age": "32",
        "gender": "女",
        "tags": "不是列表",                       # 坏类型 → 剔除
        "background": "很长" * 2000,              # 超 1500 → 截断
        "personality": {"traits": {"bad": 1}, "style": "温柔"},
        "context": {
            "specific_memories": [f"记忆{i}" for i in range(17)] + [123],
            "hobbies": ["  读书 ", "", None, "旅行"],
        },
        "tastes": {"likes": ["咖啡"] * 20, "opinions": 42},
        "selfie_scenes": ["beach"] * 9,
        "appearance": 999,                         # 坏类型 → 剔除
        "identity": {"deny_ai": True, "claim_human": True},
    }
    snapshot = copy.deepcopy(persona)
    out = pdi.clamp_persona(persona)

    assert persona == snapshot                     # 不就地改入参
    assert out["name"] == "美月"
    assert out["names"] == {"english": "Mizuki"}   # 空/未知键剔除
    assert out["age"] == 32
    assert out["gender"] == "female"
    assert "tags" not in out
    assert len(out["background"]) <= 1500
    assert out["personality"] == {"style": "温柔"}  # 坏 traits 剔除
    mems = out["context"]["specific_memories"]
    assert len(mems) == 16 and all(isinstance(m, str) for m in mems)
    assert out["context"]["hobbies"] == ["读书", "旅行"]
    assert len(out["tastes"]["likes"]) == 8 and "opinions" not in out["tastes"]
    assert len(out["selfie_scenes"]) == 6
    assert "appearance" not in out
    assert out["identity"] == {"deny_ai": True, "claim_human": True}


def test_clamp_persona_memory_item_truncation_and_empty():
    out = pdi.clamp_persona(
        {"context": {"specific_memories": ["长" * 300]}})
    assert len(out["context"]["specific_memories"][0]) <= 120
    assert pdi.clamp_persona({}) == {}
    assert pdi.clamp_persona("not a dict") == {}


# ── 3.5 context.family（亲属名册，M9 别名派生的数据源）─────────────────────

def test_clamp_persona_keeps_family_and_caps_it():
    # 超额时按 LLM 给的先后顺序截断（无排序偏好），故先放要断言的两条
    fam = {"father": "  José Leandro Navarro，建筑师  ", "mother": "名" * 300}
    fam["坏值"] = 42
    fam["空值"] = "   "
    fam.update({f"role_{i}": f"名字{i}，说明" for i in range(20)})
    out = pdi.clamp_persona({"context": {"family": fam}})["context"]["family"]

    assert len(out) <= pdi._FAMILY_MAX_ITEMS
    assert out["father"] == "José Leandro Navarro，建筑师"
    assert len(out["mother"]) <= pdi._FAMILY_VALUE_MAX_CHARS
    assert "坏值" not in out and "空值" not in out


def test_clean_flat_dict_folds_newlines_and_rejects_junk():
    """值里的换行要折平——下游 ``_head_segment`` 按换行断句，多行会让
    「姓名在首段」的约定失效，抽出来的别名就变成空。"""
    out = pdi._clean_flat_dict(
        {"father": "José Leandro\nNavarro，\n建筑师"}, 4, 24, 120)
    assert out["father"] == "José Leandro Navarro， 建筑师"
    assert pdi._clean_flat_dict(None, 4, 24, 120) == {}
    assert pdi._clean_flat_dict(["a"], 4, 24, 120) == {}


def test_family_call_is_separate_from_biography_call():
    """亲属名册单独一跳：Call2 已在 token 预算边缘（实测 33k 文档触发截断修复），
    往里塞字段会让新旧字段一起丢。"""
    assert '"family"' in pdi._FAMILY_SYSTEM
    assert "family" not in pdi._BIOGRAPHY_SYSTEM
    # 跨句关系是这次调用存在的唯一理由，提示词必须点明
    assert "跨句推断" in pdi._FAMILY_SYSTEM
    assert "ex_husband" in pdi._FAMILY_SYSTEM


def test_extract_family_soft_fails_without_breaking_extraction():
    """名册 LLM 挂掉 → 抽取整体仍成功，只记 warning（与 Call3 同一容错口径）。"""
    def chat_fn(system, user, timeout):
        if "亲属名册" in system:
            raise RuntimeError("family LLM down")
        return _two_stage_fake()(system, user, timeout)

    counter = {"n": 0}

    def seq_fn(system, user, timeout):
        counter["n"] += 1
        if counter["n"] == 1:
            return _IDENTITY_JSON
        if counter["n"] == 2:
            return _BIO_JSON
        if counter["n"] == 3:
            raise RuntimeError("family LLM down")
        return '{"sources": {}}'

    out = pdi.run_extraction("文档段落", seq_fn)
    assert out["persona"]["name"] == "美月"
    assert "family" not in (out["persona"].get("context") or {})
    assert any("亲属名册抽取失败" in w for w in out["warnings"])


def test_extract_family_bad_shape_yields_no_family():
    """LLM 回了合法 JSON 但 family 不是字典 → 静默丢弃，不写脏数据进档案。"""
    out = pdi.run_extraction("文档段落", _two_stage_fake(
        family_json='{"family": ["爸爸是建筑师"]}'))
    assert "family" not in (out["persona"].get("context") or {})


def test_family_feeds_entity_aliases_end_to_end():
    """抽取产物直接喂 M9 别名派生：跨句推断出的 ex_husband 要能溢出到「老公」。

    这一跳是整条链的收益点——文档正文一次「老公」都没有，客户却天天这么问。
    """
    from src.companion.persona_entity_alias import build_entity_aliases

    out = pdi.run_extraction("文档段落", _two_stage_fake())
    persona = dict(out["persona"], gender="female")
    aliases = build_entity_aliases(persona)
    assert "josé luis jerónimo" in {v.lower() for v in aliases.get("老公", ())}
    assert "josé luis jerónimo" in {v.lower() for v in aliases.get("前夫", ())}


# ── 4. suggested_profile_id ──────────────────────────────────────────────────

def test_suggested_profile_id_paths():
    assert pdi.suggested_profile_id(
        {"names": {"english": "Mizuki Sato"}}) == "mizuki_sato"
    assert pdi.suggested_profile_id(
        {"names": {"full_western": "Ana-Maria Lopez"}}) == "ana_maria_lopez"
    assert pdi.suggested_profile_id({"name": "Miyu 美月"}) == "miyu"
    # 纯中文名 + 无英文 → 日期兜底
    fallback = pdi.suggested_profile_id({"name": "美月"})
    assert re.fullmatch(r"imported_persona_\d{8}", fallback)
    assert re.fullmatch(r"imported_persona_\d{8}",
                        pdi.suggested_profile_id({}))


# ── 5. run_extraction 编排 ───────────────────────────────────────────────────

_IDENTITY_JSON = json.dumps({
    "name": "美月",
    "names": {"english": "Mizuki Sato", "nickname": "小月"},
    "role": "在西班牙巴塞罗那的酒店运营总监助理，32岁",
    "age": 32,
    "gender": "female",
    "tags": ["女", "32岁", "巴塞罗那", "酒店业"],
    "personality": {"traits": ["温柔", "细心"], "style": "轻声细语，爱用语气词"},
    "speaking": {"openers": ["今天过得怎么样？"]},
    "boundaries": {"topics_to_avoid": ["前男友"]},
    "appearance": "a 32-year-old East Asian woman with long dark hair",
    "warnings": ["style 属于从性格归纳"],
}, ensure_ascii=False)

_BIO_JSON = json.dumps({
    "background": "美月出生于大阪，后随家人移居巴塞罗那，现任酒店运营总监助理。",
    "context": {
        "hobbies": ["瑜伽", "烘焙"],
        "specific_memories": [
            "父亲佐藤健一是建筑师，2019 年因病去世",
            "2016 年毕业于大阪大学酒店管理专业",
        ],
        "emotional_triggers": {"positive": "聊旅行", "negative": "被质疑撒谎"},
    },
    "tastes": {"likes": ["抹茶", "海边散步"], "opinions": ["工作要认真，生活要放松"]},
    "selfie_scenes": ["hotel lobby", "beach promenade"],
    "warnings": ["selfie_scenes 由生活方式组合推断"],
}, ensure_ascii=False)


_FAMILY_JSON = json.dumps({
    "family": {
        "father": "佐藤健一，建筑师，2019 年因病去世",
        "ex_husband": "José Luis Jerónimo，西班牙人，2018 年离婚",
    },
    "warnings": [],
}, ensure_ascii=False)

_STAGE_CALLS_BEFORE_SOURCES = 3   # 身份 → 传记 → 亲属名册


def _two_stage_fake(calls_log=None, sources_json=None, sources_seq=None,
                    bio_json=None, family_json=None):
    """按调用序回 canned JSON：身份 → 传记 → 亲属名册 → 之后每次一个溯源批。

    ``sources_seq`` 按批次逐个回（用尽后回空 sources）；``sources_json`` 则每批同一份。
    """
    counter = {"n": 0}
    sources_payload = (
        sources_json if sources_json is not None else '{"sources": {}}')

    def chat_fn(system, user, timeout):
        counter["n"] += 1
        if calls_log is not None:
            calls_log.append({"system": system, "user": user})
        if counter["n"] == 1:
            return _IDENTITY_JSON
        if counter["n"] == 2:
            return bio_json if bio_json is not None else _BIO_JSON
        if counter["n"] == 3:
            return family_json if family_json is not None else _FAMILY_JSON
        if sources_seq is not None:
            idx = counter["n"] - 1 - _STAGE_CALLS_BEFORE_SOURCES
            return sources_seq[idx] if idx < len(sources_seq) else '{"sources": {}}'
        return sources_payload

    return chat_fn


def test_run_extraction_merges_and_defaults():
    calls = []
    stages = []
    out = pdi.run_extraction(
        "文档全文……", _two_stage_fake(calls),
        on_stage=lambda st, pg: stages.append((st, pg)))

    persona = out["persona"]
    assert persona["name"] == "美月"
    assert persona["role"].startswith("在西班牙巴塞罗那")
    assert persona["background"].startswith("美月出生于大阪")
    assert len(persona["context"]["specific_memories"]) == 2
    assert persona["tastes"]["likes"] == ["抹茶", "海边散步"]
    # identity 产品默认注入（非文档来源）
    assert persona["identity"] == {"deny_ai": True, "claim_human": True}
    assert any("真人身份硬锁" in w for w in out["warnings"])
    assert "style 属于从性格归纳" in out["warnings"]
    assert "selfie_scenes 由生活方式组合推断" in out["warnings"]
    assert out["suggested_id"] == "mizuki_sato"
    assert out["coverage"] == {
        "identity": True, "personality": True, "background": True,
        "memories": True, "tastes": True, "scenes": True,
    }
    assert "score" in out["completeness"]
    assert persona["context"]["family"]["ex_husband"].startswith("José Luis")
    # Call3 分批：身份批 + 记忆批（_BIO_JSON 有 2 条记忆 → 1 个记忆批）+ 收尾 tick
    assert [s for s, _ in stages] == [
        "extract_identity", "extract_biography", "extract_family",
        "extract_sources", "extract_sources", "extract_sources", "finalize"]
    src_progress = [pg for st, pg in stages if st == "extract_sources"]
    assert src_progress == sorted(src_progress)
    assert src_progress[0] == 80 and src_progress[-1] == 92
    assert len(calls) == 5
    assert "身份与性格" in calls[0]["system"]
    assert "传记与记忆" in calls[1]["system"]
    assert "亲属名册" in calls[2]["system"]
    assert "溯源标注器" in calls[3]["system"]
    assert "溯源标注器" in calls[4]["system"]
    assert calls[0]["user"].startswith("【人设包装文档全文】")


def test_run_extraction_partial_coverage():
    """只回身份、传记为空 dict → coverage 相应为 False，不崩。"""
    seq = iter([_IDENTITY_JSON, "{}", '{"sources": {}}'])

    def chat_fn(system, user, timeout):
        return next(seq)

    out = pdi.run_extraction("文档", chat_fn)
    assert out["coverage"]["identity"] is True
    assert out["coverage"]["background"] is False
    assert out["coverage"]["memories"] is False


def test_run_extraction_retry_once_on_bad_json():
    seq = iter(["oops not json", _IDENTITY_JSON, _BIO_JSON, _FAMILY_JSON,
                '{"sources": {}}', '{"sources": {}}'])
    users = []

    def chat_fn(system, user, timeout):
        users.append(user)
        return next(seq)

    out = pdi.run_extraction("文档", chat_fn)
    assert out["persona"]["name"] == "美月"
    # 身份(坏+重试) + 传记 + 亲属名册 + 溯源身份批 + 溯源记忆批
    assert len(users) == 6
    assert "不是合法 JSON" in users[1]        # 重试附加提示
    assert "不是合法 JSON" not in users[0]
    assert out["extract_meta"]["calls"][0] == {
        **out["extract_meta"]["calls"][0], "stage": "identity",
        "retried": True, "empty": False}


def test_run_extraction_raises_after_two_bad():
    def chat_fn(system, user, timeout):
        return "garbage forever"

    with pytest.raises(ValueError, match="无法解析"):
        pdi.run_extraction("文档", chat_fn)


# ── 6. 任务注册表 ────────────────────────────────────────────────────────────

def _wait_job(job_id, want_status, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = pdi.get_job(job_id)
        if job and job["status"] == want_status:
            return job
        time.sleep(0.02)
    return pdi.get_job(job_id)


def test_job_lifecycle_done():
    job_id = pdi.create_job("文档", _two_stage_fake())
    assert job_id
    job = _wait_job(job_id, "done")
    assert job["status"] == "done"
    assert job["progress"] == 100
    assert job["result"]["persona"]["name"] == "美月"
    assert job["result"]["suggested_id"] == "mizuki_sato"
    assert job["error"] == ""


def test_job_error_path():
    def chat_fn(system, user, timeout):
        raise RuntimeError("boom-llm-down")

    job_id = pdi.create_job("文档", chat_fn)
    job = _wait_job(job_id, "error")
    assert job["status"] == "error"
    assert "boom-llm-down" in job["error"]
    assert job["result"] is None


def test_job_concurrency_cap_and_unknown():
    release = threading.Event()

    def blocking_fn(system, user, timeout):
        release.wait(8)
        return _IDENTITY_JSON  # 放行后两段都回身份 JSON（能解析即可）

    ids = [pdi.create_job("文档", blocking_fn) for _ in range(pdi.MAX_RUNNING_JOBS)]
    assert all(ids)
    assert pdi.create_job("文档", blocking_fn) is None   # 满载 → None
    release.set()
    for jid in ids:
        job = _wait_job(jid, "done")
        assert job["status"] == "done"

    assert pdi.get_job("no-such-job") is None
    assert pdi.get_job("") is None


def test_job_ttl_purge(monkeypatch):
    job_id = pdi.create_job("文档", _two_stage_fake())
    _wait_job(job_id, "done")
    # 把 created_ts 拨回 TTL 之前 → get_job 触发清理 → None
    with pdi._JOBS_LOCK:
        pdi._JOBS[job_id]["created_ts"] = time.time() - pdi.JOB_TTL_SEC - 5
    assert pdi.get_job(job_id) is None


# ── 7. 路由级 ────────────────────────────────────────────────────────────────

_HDRS = {"Authorization": "Bearer test-token"}
_JSON_HDRS = {**_HDRS, "Content-Type": "application/json"}


def _build_app(tmp_path, doc_import_enabled):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {"profiles": [],
                     "doc_import": {"enabled": bool(doc_import_enabled)}},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    from src.utils.persona_manager import PersonaManager

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    PersonaManager.reset()
    from src.web.admin import create_app
    return create_app(cm)


@pytest.fixture
def app_off(tmp_path):
    yield _build_app(tmp_path, False)
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


@pytest.fixture
def app_on(tmp_path):
    yield _build_app(tmp_path, True)
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_routes_flag_off(app_off):
    async with _client(app_off) as c:
        r = await c.get("/api/personas/import-doc/status", headers=_HDRS)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "enabled": False}

        r = await c.post("/api/personas/import-doc/parse",
                         headers=_JSON_HDRS, json={"text": "hello"})
        assert r.status_code == 200
        assert r.json()["ok"] is True and r.json()["text"] == "hello"

        r = await c.post("/api/personas/import-doc/extract",
                         headers=_JSON_HDRS, json={"text": "hello"})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_routes_parse_text_and_docx(app_on):
    async with _client(app_on) as c:
        r = await c.get("/api/personas/import-doc/status", headers=_HDRS)
        assert r.json() == {"ok": True, "enabled": True}

        # JSON 文本
        r = await c.post("/api/personas/import-doc/parse",
                         headers=_JSON_HDRS,
                         json={"text": "第一段\n\n第二段\n"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["text"] == "第一段\n\n第二段"
        assert body["paragraphs"] == 2
        assert body["truncated"] is False
        assert body["text_chars"] == len(body["text"])

        # multipart docx
        data = _min_docx_bytes(["她叫美月。", "住在巴塞罗那。"])
        r = await c.post(
            "/api/personas/import-doc/parse", headers=_HDRS,
            files={"file": ("persona.docx", data,
                            "application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document")})
        assert r.status_code == 200
        assert r.json()["text"] == "她叫美月。\n住在巴塞罗那。"

        # multipart txt
        r = await c.post(
            "/api/personas/import-doc/parse", headers=_HDRS,
            files={"file": ("persona.txt", "纯文本人设".encode("utf-8"),
                            "text/plain")})
        assert r.status_code == 200
        assert r.json()["text"] == "纯文本人设"

        # 坏后缀 → 400；空文本 → 400；超 5MB → 413
        r = await c.post(
            "/api/personas/import-doc/parse", headers=_HDRS,
            files={"file": ("evil.pdf", b"%PDF", "application/pdf")})
        assert r.status_code == 400
        r = await c.post("/api/personas/import-doc/parse",
                         headers=_JSON_HDRS, json={"text": "   "})
        assert r.status_code == 400
        r = await c.post(
            "/api/personas/import-doc/parse", headers=_HDRS,
            files={"file": ("big.txt", b"a" * (5 * 1024 * 1024 + 1),
                            "text/plain")})
        assert r.status_code == 413


@pytest.mark.asyncio
async def test_routes_extract_job_roundtrip(app_on):
    app_on.state.persona_doc_chat_fn = _two_stage_fake()   # 可测缝：注入假 LLM
    async with _client(app_on) as c:
        r = await c.post("/api/personas/import-doc/extract",
                         headers=_JSON_HDRS, json={"text": "人设文档全文……"})
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        assert job_id

        job = None
        for _ in range(200):
            r = await c.get(f"/api/personas/import-doc/jobs/{job_id}",
                            headers=_HDRS)
            assert r.status_code == 200
            job = r.json()["job"]
            if job["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.02)
        assert job["status"] == "done"
        assert job["progress"] == 100
        assert job["result"]["persona"]["name"] == "美月"
        assert job["result"]["persona"]["identity"]["deny_ai"] is True
        assert job["result"]["suggested_id"] == "mizuki_sato"

        r = await c.get("/api/personas/import-doc/jobs/nope", headers=_HDRS)
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_routes_extract_validation_and_busy(app_on, monkeypatch):
    app_on.state.persona_doc_chat_fn = _two_stage_fake()
    async with _client(app_on) as c:
        # 空文本 → 400
        r = await c.post("/api/personas/import-doc/extract",
                         headers=_JSON_HDRS, json={"text": ""})
        assert r.status_code == 400
        # 超 MAX_TEXT_CHARS → 413
        r = await c.post("/api/personas/import-doc/extract",
                         headers=_JSON_HDRS,
                         json={"text": "x" * (pdi.MAX_TEXT_CHARS + 1)})
        assert r.status_code == 413
        # 注册表满载 → 429
        monkeypatch.setattr(pdi, "create_job", lambda text, fn: None)
        r = await c.post("/api/personas/import-doc/extract",
                         headers=_JSON_HDRS, json={"text": "正常文本"})
        assert r.status_code == 429


# ── 8. 生产配置可解析 ────────────────────────────────────────────────────────

def test_production_yaml_parse_and_new_section():
    # config.yaml 被 gitignore；CI 读出厂 config.example.yaml。
    path = _ENGINE_ROOT / "config" / "config.yaml"
    if not path.is_file():
        path = _ENGINE_ROOT / "config" / "config.example.yaml"
    base = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(base, dict)
    sect = (base.get("personas") or {}).get("doc_import") or {}
    assert sect.get("enabled") is False          # 基线默认关

    local_path = _ENGINE_ROOT / "config" / "config.local.yaml"
    if local_path.exists():                      # CI 无 overlay 时跳过内容断言
        local = yaml.safe_load(local_path.read_text(encoding="utf-8"))
        assert isinstance(local, dict)
        sect = (local.get("personas") or {}).get("doc_import") or {}
        assert isinstance(sect.get("enabled"), bool)


# ── 9. 溯源标注（H2：Call3 独立轻量 sources）─────────────────────────────────

def test_split_numbered_paragraphs_matches_count():
    samples = [
        "第一段\n\n第二段\n第三段",
        "a\r\n\n\n\nb  \r\nc\n",     # 原始杂乱文本也同口径
        "",
        "   \n  \n",
        "单段",
    ]
    for text in samples:
        paras = pdi.split_numbered_paragraphs(text)
        assert len(paras) == pdi.count_paragraphs(text)   # 编号一致性不变量
        assert all(p == p.strip() and p for p in paras)

    assert pdi.split_numbered_paragraphs("第一段\n\n第二段") == ["第一段", "第二段"]
    assert pdi.split_numbered_paragraphs(None) == []


def test_extraction_prompts_call12_no_sources_call3_sources():
    """Call1/2/2b 不要求 sources；Call3 分批溯源 prompt + 本批摘要/编号段落。"""
    calls = []
    pdi.run_extraction("第一段身份信息\n\n第二段传记信息", _two_stage_fake(calls))
    assert len(calls) == 5
    for call in calls[:3]:
        assert call["user"].startswith("【人设包装文档全文】")
        assert "[1] 第一段身份信息" in call["user"]
        assert "[2] 第二段传记信息" in call["user"]
        # Call1/2/2b：明确禁 sources；无 '"sources":' 强制示例块
        assert "不要输出 sources" in call["system"]
        assert '"sources":' not in call["system"]
    # 亲属名册单源：只有 Call2b 要 family，Call2 不许再要（两处都要 = 双源真相）
    assert '"family"' in calls[2]["system"]
    assert '"family"' not in calls[1]["system"]

    identity_batch, memory_batch = calls[3], calls[4]
    for call in (identity_batch, memory_batch):
        assert "溯源标注器" in call["system"]
        assert "宁缺勿错" in call["system"]
        assert call["user"].startswith("【本批待溯源内容】")
        assert "【编号段落】" in call["user"]
        assert "[1] 第一段身份信息" in call["user"]
    # 身份批：概览字段、明令不带记忆键；记忆批：逐条 index 键
    assert "身份与概览字段" in identity_batch["system"]
    assert ('"name"' in identity_batch["user"]
            or '"background"' in identity_batch["user"])
    assert "specific_memories" not in identity_batch["user"]
    assert "具体记忆" in memory_batch["system"]
    assert "context.specific_memories.<index>" in memory_batch["system"]
    assert '"index"' in memory_batch["user"]


def test_run_extraction_sources_roundtrip_and_validation():
    """Call3 逐条记忆 + 字段级回落；非法编号/越界下标剔除；excerpts 仅被引用段。"""
    long_para = "长" * 600
    doc = f"美月的身份信息。\n{long_para}\n第三段：家庭档案。"

    identity_payload = {
        "sources": {
            "name": [1],
            "role": [2, 1],
            "background": [3, 3, 2],                   # 去重保序
            "boundaries.topics_to_avoid": [0, 99, "3", None, True],
            "": [1],
            "personality": "not-a-list",
            "context.specific_memories.0": [1],        # 身份批越界键 → 丢
        }
    }
    memory_payload = {
        "sources": {
            "context.specific_memories.0": [3],
            "context.specific_memories.1": [3, 99],    # 99 非法剔除
            "context.specific_memories.9": [1],        # 越界下标 → 整键丢
            "context.specific_memories": [3],          # 字段级整组回落仍兼容
            "name": [2],                               # 记忆批串批键 → 丢
        }
    }

    out = pdi.run_extraction(doc, _two_stage_fake(sources_seq=[
        json.dumps(identity_payload, ensure_ascii=False),
        json.dumps(memory_payload, ensure_ascii=False)]))

    assert out["paragraphs_total"] == 3
    assert out["sources"] == {
        "name": [1],
        "role": [2, 1],
        "background": [3, 2],
        "context.specific_memories.0": [3],
        "context.specific_memories.1": [3],
        "context.specific_memories": [3],
    }
    assert "context.specific_memories.9" not in out["sources"]
    ex = out["source_excerpts"]
    assert set(ex.keys()) == {"1", "2", "3"}      # 仅被引用编号
    assert ex["1"] == "美月的身份信息。"
    assert ex["3"] == "第三段：家庭档案。"
    assert ex["2"] == "长" * 500 + "…"            # 超 500 截断加 …
    assert not any("溯源标注未生成" in w for w in out["warnings"])
    assert out["persona"]["name"] == "美月"       # 抽取主体不受影响


def test_run_extraction_without_sources_tolerated():
    calls = []
    out = pdi.run_extraction("文档段落一\n文档段落二", _two_stage_fake(calls))

    assert out["sources"] == {}
    assert out["source_excerpts"] == {}
    assert out["paragraphs_total"] == 2
    assert any("溯源标注未生成" in w for w in out["warnings"])
    assert out["persona"]["name"] == "美月"       # 抽取整体成功
    # 2 批（身份 + 记忆）各一次调用：空 sources ≠ 坏 JSON，不触发重试
    assert len(calls) == 5
    assert out["extract_meta"]["sources_batches"] == {
        "total": 2, "ok": 0, "empty": 2}


def test_run_extraction_call3_error_soft_fail():
    """Call3 抛错 → 抽取仍成功，sources={}，warnings 含提示。"""
    counter = {"n": 0}

    def chat_fn(system, user, timeout):
        counter["n"] += 1
        if counter["n"] == 1:
            return _IDENTITY_JSON
        if counter["n"] == 2:
            return _BIO_JSON
        raise RuntimeError("sources LLM down")

    out = pdi.run_extraction("文档段落一", chat_fn)
    assert out["persona"]["name"] == "美月"
    assert out["sources"] == {} and out["source_excerpts"] == {}
    assert any("溯源标注未生成" in w for w in out["warnings"])


def test_run_extraction_all_invalid_sources_treated_as_missing():
    sources_payload = json.dumps({
        "sources": {"name": [999], "role": "oops",
                    "context.specific_memories.0": [0, -1, True]},
    })
    out = pdi.run_extraction(
        "唯一一段", _two_stage_fake(sources_json=sources_payload))

    assert out["sources"] == {} and out["source_excerpts"] == {}
    assert any("溯源标注未生成" in w for w in out["warnings"])
    assert any("第 1 批溯源未生成" in w for w in out["warnings"])


def test_run_extraction_field_level_sources_still_accepted():
    """旧字段级 sources（无逐条记忆键）仍兼容：概览键出自身份批、整组回落出自记忆批。"""
    out = pdi.run_extraction("唯一一段家庭档案", _two_stage_fake(sources_seq=[
        json.dumps({"sources": {"background": [1], "tastes.likes": [1]}}),
        json.dumps({"sources": {"context.specific_memories": [1]}}),
    ]))
    assert out["sources"] == {
        "background": [1],
        "context.specific_memories": [1],
        "tastes.likes": [1],
    }
    assert not any("溯源标注未生成" in w for w in out["warnings"])


def test_job_result_includes_sources_keys():
    """create_job 路径的 result 契约：三个溯源新键随任务结果透出（路由零改动可见）。"""
    job_id = pdi.create_job("文档", _two_stage_fake())
    job = _wait_job(job_id, "done")
    assert job["status"] == "done"
    result = job["result"]
    assert result["sources"] == {}                # Call3 默认空 sources
    assert result["source_excerpts"] == {}
    assert result["paragraphs_total"] == 1


# ── 10. create_job_runner 通用后台任务（D1 线，供兄弟线 E 复用）──────────────

def test_create_job_runner_lifecycle_done():
    def runner(on_stage):
        on_stage("consistency_probe", 42)
        return {"kind": "quiz", "items": [1, 2, 3]}

    job_id = pdi.create_job_runner(runner)
    assert job_id
    job = _wait_job(job_id, "done")
    assert job["status"] == "done"
    assert job["progress"] == 100
    assert job["result"] == {"kind": "quiz", "items": [1, 2, 3]}
    assert job["error"] == ""


def test_create_job_runner_stage_updates_visible_while_running():
    entered = threading.Event()
    release = threading.Event()

    def runner(on_stage):
        on_stage("halfway", 50)
        entered.set()
        release.wait(8)
        return {"ok": True}

    job_id = pdi.create_job_runner(runner)
    try:
        assert entered.wait(8)
        job = pdi.get_job(job_id)
        assert job["status"] == "running"
        assert job["stage"] == "halfway" and job["progress"] == 50
    finally:
        release.set()
    job = _wait_job(job_id, "done")
    assert job["result"] == {"ok": True}


def test_create_job_runner_error_path():
    def runner(on_stage):
        raise RuntimeError("boom-runner")

    job_id = pdi.create_job_runner(runner)
    job = _wait_job(job_id, "error")
    assert job["status"] == "error"
    assert "boom-runner" in job["error"]
    assert job["result"] is None


def _mem_bio_json(n):
    """造一份含 n 条可区分记忆的传记 JSON（记忆文本互不为子串）。"""
    return json.dumps({
        "background": "美月的履历浓缩。",
        "context": {"specific_memories": [f"记忆事实第{i:02d}号" for i in range(n)]},
    }, ensure_ascii=False)


# ── 11. Call3 分批溯源 / 粗筛 / 重试 / extract_meta（I2 线）───────────────────

def test_preselect_paragraphs_relevance_first_and_limit():
    paras = [
        "今天天气不错，随手记一笔。",
        "父亲佐藤健一是建筑师，长年在大阪工作。",
        "一些与人设无关的闲聊内容。",
        "2016 年毕业于大阪大学酒店管理专业。",
        "又一段与人设无关的填充文字。",
    ]
    targets = ["父亲佐藤健一是建筑师，2019 年因病去世",
               "2016 年毕业于大阪大学酒店管理专业"]
    out = pdi.preselect_paragraphs(paras, targets, limit=3)

    assert len(out) == 3
    assert {n for n, _ in out[:2]} == {2, 4}          # 两个相关段排前（按分降序）
    assert out[2][0] in (1, 3, 5)                     # 无关段只作填充
    assert all(paras[n - 1] == t for n, t in out)     # 编号保持 1-based 原值


def test_preselect_paragraphs_latin_and_year_tokens():
    paras = ["random filler line here", "She works in Barcelona hotels",
             "another unrelated english line"]
    out = pdi.preselect_paragraphs(paras, ["hotel operations in Barcelona"],
                                   limit=1)
    assert out == [(2, "She works in Barcelona hotels")]

    paras2 = ["无关段", "她在 2019 年搬到巴塞罗那"]
    assert pdi.preselect_paragraphs(paras2, ["2019 年发生的事"], limit=1) == [
        (2, "她在 2019 年搬到巴塞罗那")]


def test_preselect_paragraphs_empty_and_no_targets():
    assert pdi.preselect_paragraphs([], ["目标"]) == []
    assert pdi.preselect_paragraphs(None, None) == []
    # 无目标词 → 原顺序前 N 段
    assert pdi.preselect_paragraphs(["甲段", "乙段", "丙段"], [], limit=2) == [
        (1, "甲段"), (2, "乙段")]
    # 空行跳过但编号不重排
    assert pdi.preselect_paragraphs(["", "第二段有内容"], []) == [
        (2, "第二段有内容")]
    # limit<=0 = 不限量
    assert len(pdi.preselect_paragraphs(["甲", "乙", "丙"], [], limit=0)) == 3


def test_plan_source_batches_splits_memories_by_six():
    mems = [f"记忆{i}" for i in range(16)]
    batches = pdi._plan_source_batches(
        {"name": "美月", "context": {"specific_memories": mems}})

    assert [b["kind"] for b in batches] == [
        "identity", "memories", "memories", "memories"]
    assert [sorted(b["mem_indices"]) for b in batches[1:]] == [
        list(range(0, 6)), list(range(6, 12)), list(range(12, 16))]
    assert batches[0]["mem_indices"] == set()
    assert [b["stage"] for b in batches] == [
        "sources_identity", "sources_memories_1",
        "sources_memories_2", "sources_memories_3"]
    # 无记忆 → 只有身份批；空人设 → 一批都不建（省调用）
    assert [b["kind"] for b in pdi._plan_source_batches({"name": "美月"})] == [
        "identity"]
    assert pdi._plan_source_batches({}) == []


def test_sources_batches_user_contains_only_own_memories():
    calls = []
    doc = "\n".join(f"文档第{i}段：与人设相关的叙述内容。" for i in range(1, 21))
    pdi.run_extraction(doc, _two_stage_fake(calls, bio_json=_mem_bio_json(16)))

    src_calls = calls[_STAGE_CALLS_BEFORE_SOURCES:]
    assert len(src_calls) == 4                       # 身份批 + 16/6 → 3 记忆批
    for i, call in enumerate(src_calls[1:]):
        batch_idx = list(range(i * 6, min(i * 6 + 6, 16)))
        for j in range(16):
            token = f"记忆事实第{j:02d}号"
            assert (token in call["user"]) is (j in batch_idx)
        assert f'"index": {batch_idx[0]}' in call["user"]   # 全局 index 不重编号


def test_sources_one_empty_batch_does_not_break_others():
    """某批返回空 → 其余批 sources 仍合并，warnings 点名该批，整体不失败。"""
    out = pdi.run_extraction("家庭档案段落一\n工作经历段落二", _two_stage_fake(
        sources_seq=[
            json.dumps({"sources": {"name": [1], "background": [2]}}),
            "not json at all",              # 记忆批首答坏 JSON
            "still not json",               # 重试仍坏 → 该批丢
        ]))

    assert out["sources"] == {"name": [1], "background": [2]}
    assert any("第 2 批溯源未生成" in w for w in out["warnings"])
    assert not any("第 1 批溯源未生成" in w for w in out["warnings"])
    assert not any("溯源标注未生成" == w for w in out["warnings"])
    assert out["persona"]["name"] == "美月"
    assert out["extract_meta"]["sources_batches"] == {
        "total": 2, "ok": 1, "empty": 1}


def test_sources_empty_response_retries_with_shorter_input():
    """首次空响应 → 第二次输入更短（段落减半 + 摘要截短）→ 成功。"""
    calls = []
    doc = "\n".join(
        f"第{i}段：美月的家庭与工作叙述，内容较长以便观察预算裁剪效果。"
        for i in range(1, 31))
    out = pdi.run_extraction(doc, _two_stage_fake(calls, sources_seq=[
        "   ",                                        # 身份批首答空白
        json.dumps({"sources": {"name": [1]}}),       # 缩短输入后成功
    ]))

    first_user = calls[_STAGE_CALLS_BEFORE_SOURCES]["user"]
    retry_user = calls[_STAGE_CALLS_BEFORE_SOURCES + 1]["user"]
    assert len(retry_user) < len(first_user)
    assert "不是合法 JSON" not in retry_user           # 空响应走缩短分支，非坏 JSON 分支
    assert out["sources"] == {"name": [1]}
    assert any("第 1 批溯源首次空响应" in w for w in out["warnings"])

    identity_call = out["extract_meta"]["calls"][_STAGE_CALLS_BEFORE_SOURCES]
    assert identity_call["stage"] == "sources_identity"
    assert identity_call["retried"] is True and identity_call["empty"] is True


def test_sources_bad_json_batch_retry_uses_suffix_not_shrink():
    calls = []
    out = pdi.run_extraction("段落一\n段落二", _two_stage_fake(calls, sources_seq=[
        "oops not json",
        json.dumps({"sources": {"role": [1]}}),
    ]))
    assert "不是合法 JSON" in calls[_STAGE_CALLS_BEFORE_SOURCES + 1]["user"]
    assert out["sources"] == {"role": [1]}
    first_src = out["extract_meta"]["calls"][_STAGE_CALLS_BEFORE_SOURCES]
    assert first_src == {**first_src, "retried": True, "empty": False}


def test_extract_meta_contract_and_memory_counts():
    out = pdi.run_extraction("家庭档案段落", _two_stage_fake(sources_seq=[
        json.dumps({"sources": {"name": [1]}}),
        json.dumps({"sources": {"context.specific_memories.1": [1]}}),
    ]))

    meta = out["extract_meta"]
    assert [c["stage"] for c in meta["calls"]] == [
        "identity", "biography", "family",
        "sources_identity", "sources_memories_1"]
    for call in meta["calls"]:
        assert set(call) == {"stage", "ms", "retried", "empty",
                             "attempts", "repaired"}
        assert isinstance(call["ms"], int) and call["ms"] >= 0
        assert call["retried"] is False and call["empty"] is False
        assert call["attempts"] == 1 and call["repaired"] is False
    assert meta["sources_batches"] == {"total": 2, "ok": 2, "empty": 0}
    assert meta["memories_total"] == 2 and meta["memories_sourced"] == 1
    assert isinstance(meta["total_ms"], int) and meta["total_ms"] >= 0


def test_extract_meta_memories_sourced_counts_per_item_keys_only():
    out = pdi.run_extraction("段落", _two_stage_fake(sources_seq=[
        '{"sources": {}}',
        json.dumps({"sources": {"context.specific_memories": [1]}}),
    ]))
    # 整组回落键不计入逐条覆盖
    assert out["sources"] == {"context.specific_memories": [1]}
    assert out["extract_meta"]["memories_sourced"] == 0
    assert out["extract_meta"]["memories_total"] == 2


def test_sources_batch_tolerates_missing_wrapper():
    """批响应漏包一层（直接给映射）也认；给了 sources 但类型不对则不猜。"""
    assert pdi._unwrap_sources({"sources": {"name": [1]}}) == {"name": [1]}
    assert pdi._unwrap_sources({"name": [1]}) == {"name": [1]}
    assert pdi._unwrap_sources({"sources": "oops"}) is None
    assert pdi._unwrap_sources({"note": "文字解释"}) is None
    assert pdi._unwrap_sources({}) is None
    assert pdi._unwrap_sources("not a dict") is None

    out = pdi.run_extraction("唯一一段", _two_stage_fake(
        sources_json='{"name": [1]}'))
    assert out["sources"] == {"name": [1]}


def test_job_result_includes_extract_meta():
    job_id = pdi.create_job("文档", _two_stage_fake())
    job = _wait_job(job_id, "done")
    assert job["status"] == "done"
    assert set(job["result"]["extract_meta"]) == {
        "calls", "sources_batches", "memories_sourced", "memories_total",
        "total_ms"}


# ── 12. JSON mode 透传（chat_fn 向后兼容探测）────────────────────────────────

def test_chat_fn_accepts_json_mode_detection():
    def legacy(system, user, timeout):
        return ""

    def with_kwarg(system, user, timeout, response_format=None):
        return ""

    def with_varkw(system, user, timeout, **kw):
        return ""

    assert pdi._chat_fn_accepts_json_mode(legacy) is False
    assert pdi._chat_fn_accepts_json_mode(with_kwarg) is True
    assert pdi._chat_fn_accepts_json_mode(with_varkw) is True


def test_sources_calls_pass_json_mode_when_supported():
    """chat_fn 支持 response_format 时：只有 Call3 各批透传 json_object。"""
    seen = []
    inner = _two_stage_fake()

    def chat_fn(system, user, timeout, response_format=None):
        seen.append(response_format)
        return inner(system, user, timeout)

    pdi.run_extraction("段落", chat_fn)
    assert len(seen) == 5
    # Call1/Call2/Call2b 不动
    assert seen[:_STAGE_CALLS_BEFORE_SOURCES] == [None] * _STAGE_CALLS_BEFORE_SOURCES
    assert all(rf == {"type": "json_object"}
               for rf in seen[_STAGE_CALLS_BEFORE_SOURCES:])


def test_legacy_three_arg_chat_fn_still_works():
    """不支持 json mode 的旧三参 chat_fn（生产口径）零改动可用。"""
    out = pdi.run_extraction("段落", _two_stage_fake(
        sources_json=json.dumps({"sources": {"name": [1]}})))
    assert out["sources"] == {"name": [1]}


# ── 13. Call1/Call2 分类重试（空响应 vs 坏 JSON）+ JSON 截断修复（I2 续）─────

@pytest.fixture
def fake_sleep(monkeypatch):
    """退避改为记录：单测绝不真 sleep（返回的 list 即调用参数序列）。"""
    slept = []
    monkeypatch.setattr(pdi, "_sleep", lambda s: slept.append(s))
    return slept


def _scripted_chat(responses, users=None):
    """按脚本逐次返回响应的假 chat_fn（用尽即 StopIteration = 多调用了就红）。"""
    seq = iter(responses)

    def chat_fn(system, user, timeout):
        if users is not None:
            users.append(user)
        return next(seq)

    return chat_fn


def test_call_llm_json_empty_response_retries_verbatim(fake_sleep):
    """空响应 → **原样**重试（不带「上次不是合法 JSON」误导话术）→ 成功。"""
    users, meta = [], []
    out = pdi._call_llm_json(
        _scripted_chat(["", _IDENTITY_JSON], users),
        "SYS", "USER-DOC", "传记与记忆", stage="biography", calls_meta=meta)

    assert out["name"] == "美月"
    assert users == ["USER-DOC", "USER-DOC"]
    assert pdi._RETRY_SUFFIX not in users[1]
    assert fake_sleep == [1.5]
    assert meta[0]["attempts"] == 2 and meta[0]["retried"] is True
    assert meta[0]["empty"] is True and meta[0]["repaired"] is False


def test_call_llm_json_two_empties_then_success(fake_sleep):
    users, meta = [], []
    out = pdi._call_llm_json(
        _scripted_chat(["", "  \n ", _BIO_JSON], users),
        "SYS", "U", "传记与记忆", stage="biography", calls_meta=meta)

    assert out["background"].startswith("美月出生于大阪")
    assert users == ["U", "U", "U"]                  # 三次都原样
    assert fake_sleep == [1.5, 3.0]                  # 退避递增
    assert meta[0]["attempts"] == 3 and meta[0]["empty"] is True


def test_call_llm_json_three_empties_raises_with_attempts(fake_sleep):
    users, meta = [], []
    with pytest.raises(ValueError, match="无法解析"):
        pdi._call_llm_json(_scripted_chat(["", "", ""], users),
                           "SYS", "U", "传记与记忆", stage="biography",
                           calls_meta=meta)

    assert users == ["U", "U", "U"]                  # 上限 3 次，不无限重试
    assert fake_sleep == [1.5, 3.0]
    assert meta[0]["attempts"] == 3 and meta[0]["empty"] is True


def test_call_llm_json_mixed_empty_then_bad_json(fake_sleep):
    """混合：首次空（原样重试）→ 第二次坏 JSON（附提示）→ 第三次成功。"""
    users, meta = [], []
    out = pdi._call_llm_json(
        _scripted_chat(["", "解释一下：这里是我的思路……", _IDENTITY_JSON], users),
        "SYS", "U", "身份与性格", stage="identity", calls_meta=meta)

    assert out["name"] == "美月"
    assert users[0] == "U" and users[1] == "U"
    assert users[2] == "U" + pdi._RETRY_SUFFIX
    assert fake_sleep == [1.5]                       # 只为空响应退避
    assert meta[0]["attempts"] == 3 and meta[0]["empty"] is True


def test_call_llm_json_bad_json_then_empty_falls_back_to_verbatim(fake_sleep):
    """坏 JSON（带提示）→ 空响应 → 回到原样重试（空响应不该继续背误导话术）。"""
    users, meta = [], []
    out = pdi._call_llm_json(
        _scripted_chat(["oops not json", "", _BIO_JSON], users),
        "SYS", "U", "传记与记忆", stage="biography", calls_meta=meta)

    assert out["background"].startswith("美月出生于大阪")
    assert users[1] == "U" + pdi._RETRY_SUFFIX
    assert users[2] == "U"
    assert fake_sleep == [1.5]
    assert meta[0]["attempts"] == 3 and meta[0]["empty"] is True


def test_call_llm_json_bad_json_budget_unchanged(fake_sleep):
    """坏 JSON 仍只重试 1 次（2 次调用后放弃），且不退避。"""
    users, meta = [], []
    with pytest.raises(ValueError, match="无法解析"):
        pdi._call_llm_json(_scripted_chat(["garbage", "still garbage"], users),
                           "SYS", "U", "身份与性格", stage="identity",
                           calls_meta=meta)

    assert len(users) == 2 and fake_sleep == []
    assert meta[0]["attempts"] == 2 and meta[0]["empty"] is False


def test_run_extraction_survives_transient_empty_response(fake_sleep):
    """真实事故复现：Call2 首答空（云端抖动）→ 原样重试成功 → 整轮不再硬失败。"""
    users = []
    out = pdi.run_extraction("唯一一段", _scripted_chat(
        [_IDENTITY_JSON, "", _BIO_JSON, '{"sources": {}}', '{"sources": {}}'],
        users))

    assert out["persona"]["background"].startswith("美月出生于大阪")
    bio_call = out["extract_meta"]["calls"][1]
    assert bio_call["stage"] == "biography"
    assert bio_call["attempts"] == 2 and bio_call["empty"] is True
    assert users[1] == users[2]                      # 传记 prompt 原样复发
    assert pdi._RETRY_SUFFIX not in users[2]
    assert fake_sleep == [1.5]


# ── JSON 截断修复 ────────────────────────────────────────────────────────────

def test_repair_truncated_json_missing_closers():
    raw = ('{"background": "履历浓缩", "context": {"specific_memories": '
           '["父亲是建筑师", "2016 年毕业于大阪大学"')
    assert pdi._parse_llm_json(raw) == {
        "background": "履历浓缩",
        "context": {"specific_memories": ["父亲是建筑师", "2016 年毕业于大阪大学"]},
    }
    # 只缺一个尾 ] 的数组同理
    assert pdi._parse_llm_json('{"tags": ["女", "32岁"') == {
        "tags": ["女", "32岁"]}


def test_repair_truncated_json_drops_trailing_half_pair():
    # 半个键值对 / 半个数字都丢掉，只保留最后一个完整值
    assert pdi._parse_llm_json('{"age": 32, "role": "写到一半就被截') == {"age": 32}
    assert pdi._parse_llm_json('{"name": "美月", "age": 3') == {"name": "美月"}
    assert pdi._parse_llm_json('{"a": {"b": 1}, "c": [') == {"a": {"b": 1}}


def test_repair_truncated_json_ignores_braces_inside_strings():
    """字符串字面量里的 {}/[] 不参与配平——否则正文括号会把栈算错。"""
    raw = '{"quirks": "她爱说「{ 括号 }」和 [中括号] 这种梗", "age": 32'
    assert pdi._parse_llm_json(raw) == {
        "quirks": "她爱说「{ 括号 }」和 [中括号] 这种梗"}

    raw2 = '{"note": "转义引号 \\" 与 { 都在串里", "next": ["x"'
    assert pdi._parse_llm_json(raw2) == {
        "note": '转义引号 " 与 { 都在串里', "next": ["x"]}

    # 串里的 } 让 rfind('}') 落在串内（常规解析必失败）→ 仍能靠修复救回
    raw3 = '{"a": "含 } 的正文", "b": "第二段被截'
    assert pdi._parse_llm_json(raw3) == {"a": "含 } 的正文"}


def test_repair_truncated_json_conservative_failures():
    """结构错乱 / 无任何完整值 / 修复后仍不合法 → 照旧失败，不返回半成品。"""
    assert pdi._parse_llm_json('{"a": [1, 2}') is None        # 括号类型错配
    assert pdi._parse_llm_json('{"only_key": ') is None       # 无完整值边界
    assert pdi._parse_llm_json('{"a": "未闭合且前面没有完整值') is None
    assert pdi._repair_truncated_json("完全没有大括号") is None
    assert pdi._repair_truncated_json("") is None
    # 修复出的候选串仍非法（字符串里有裸控制字符）→ 不硬塞
    assert pdi._parse_llm_json('{"a": "换\n行", "b": 1') is None


def test_repair_does_not_kick_in_for_healthy_json():
    for raw in ('{"a": 1}', '```json\n{"a": 1}\n```', '{"a": 1, "b": [1, 2,],}'):
        assert pdi._parse_llm_json_ex(raw)[1] is False
    assert pdi._parse_llm_json_ex('{"a": 1}')[0] == {"a": 1}
    assert pdi._parse_llm_json_ex("")[0] is None


def test_run_extraction_repairs_truncated_biography(fake_sleep):
    """Call2 被 max_tokens 截断 → 修复尾部继续跑，并 warning 提示字段可能不全。"""
    truncated_bio = ('{"background": "美月出生于大阪，后移居巴塞罗那。", '
                     '"context": {"specific_memories": ["父亲是建筑师", '
                     '"2016 年毕业于大阪大学"')
    out = pdi.run_extraction(
        "唯一一段", _two_stage_fake(bio_json=truncated_bio))

    persona = out["persona"]
    assert persona["background"].startswith("美月出生于大阪")
    assert persona["context"]["specific_memories"] == [
        "父亲是建筑师", "2016 年毕业于大阪大学"]
    assert any("已修复尾部" in w for w in out["warnings"])
    bio_call = out["extract_meta"]["calls"][1]
    assert bio_call["stage"] == "biography"
    assert bio_call["repaired"] is True and bio_call["attempts"] == 1
    assert fake_sleep == []                          # 修复即成，无需重试


def test_sources_batch_records_attempts_and_repaired():
    """溯源批同样记 attempts/repaired；截断的批响应也能修复并计数。"""
    out = pdi.run_extraction("唯一一段家庭档案", _two_stage_fake(sources_seq=[
        '{"sources": {"name": [1], "role": [1], "background": [1',  # 身份批被截断
        "oops not json",                                    # 记忆批坏 JSON
        json.dumps({"sources": {"context.specific_memories.0": [1]}}),
    ]))

    # 尾部未终止的数字（可能是 12 被截半）保守丢弃，前面完整的键全部救回
    assert out["sources"] == {
        "name": [1], "role": [1], "context.specific_memories.0": [1]}
    identity_call, memory_call = (
        out["extract_meta"]["calls"][_STAGE_CALLS_BEFORE_SOURCES:])
    assert identity_call["attempts"] == 1 and identity_call["repaired"] is True
    assert memory_call["attempts"] == 2 and memory_call["repaired"] is False
    assert any("已修复尾部" in w for w in out["warnings"])


def test_create_job_runner_cap_shared_with_create_job():
    release = threading.Event()

    def blocking_runner(on_stage):
        release.wait(8)
        return {}

    ids = [pdi.create_job_runner(blocking_runner)
           for _ in range(pdi.MAX_RUNNING_JOBS)]
    assert all(ids)
    assert pdi.create_job_runner(blocking_runner) is None    # 满载 → None
    # create_job 与 create_job_runner 共用同一注册表/上限
    assert pdi.create_job("文档", _two_stage_fake()) is None
    release.set()
    for jid in ids:
        assert _wait_job(jid, "done")["status"] == "done"
