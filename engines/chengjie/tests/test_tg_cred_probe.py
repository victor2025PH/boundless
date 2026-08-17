"""凭据池真伪探针（scripts/tg_cred_probe.py）纯函数门禁。

探针的存在理由：website 池上架只验格式（32 位 hex）不验真伪，废组会被粘定派发
给新装机（2026-08-10 API_ID_INVALID 实锤）。真探测需要网络，这里只钉纯函数：
解析必须与 website parseCreds 语义一致、分类不误伤（限流/网络问题不得判 bad）、
exit code 契约（1=门禁挡、2=探针环境坏也必须响）。
"""

from __future__ import annotations

import asyncio

from scripts.tg_cred_probe import (
    classify_probe_error,
    mask_hash,
    parse_pool_creds,
    parse_proxy_arg,
    summarize,
)

_HASH = "0123456789abcdef0123456789abcdef"


# ── 解析：语义镜像 website/lib/tg-cred-pool.ts::parseCreds ───────────────────

def test_parse_pool_creds_valid_and_defaults():
    raw = f'[{{"api_id":"123456","api_hash":"{_HASH}","max":50,"name":"grp-1"}}]'
    got = parse_pool_creds(raw)
    assert len(got) == 1 and got[0]["name"] == "grp-1" and got[0]["api_id"] == "123456"
    # name 缺省 → api-<id>（与 website 同规则）
    got2 = parse_pool_creds(f'[{{"api_id":"123456","api_hash":"{_HASH.upper()}"}}]')
    assert got2[0]["name"] == "api-123456"  # hash 大小写不敏感也一并验证


def test_parse_pool_creds_rejects_malformed_entries():
    bad = [
        '{"api_id":"123456"}',                        # 非数组
        'not json',
        f'[{{"api_id":"12x456","api_hash":"{_HASH}"}}]',   # api_id 非纯数字
        f'[{{"api_id":"123","api_hash":"{_HASH}"}}]',      # api_id 太短（<4）
        f'[{{"api_id":"123456","api_hash":"{_HASH[:-1]}"}}]',  # hash 31 位
        '[{"api_id":"123456","api_hash":"zz" }]',
        '[42, null]',
    ]
    for raw in bad:
        assert parse_pool_creds(raw) == [], f"非法条目未被剔除: {raw}"


def test_parse_pool_creds_skips_bad_keeps_good():
    raw = (f'[{{"api_id":"bad","api_hash":"{_HASH}"}},'
           f'{{"api_id":"654321","api_hash":"{_HASH}","name":"ok"}}]')
    got = parse_pool_creds(raw)
    assert [c["name"] for c in got] == ["ok"]


# ── 分类：限流/网络问题绝不能误判成「凭据废」──────────────────────────────────

class FloodWait(Exception):
    pass


def test_classify_probe_error_buckets():
    assert classify_probe_error(Exception(
        "[400 API_ID_INVALID] - The api_id/api_hash combination is invalid")) == "bad"
    assert classify_probe_error(Exception("[400 API_ID_PUBLISHED_FLOOD] ...")) == "bad"
    assert classify_probe_error(FloodWait("[420 FLOOD_WAIT_X]")) == "rate_limited"
    assert classify_probe_error(TimeoutError()) == "unreachable"
    assert classify_probe_error(Exception("[Errno 11001] getaddrinfo failed")) == "unreachable"
    assert classify_probe_error(ValueError("boom")) == "error"


# ── exit code 契约 ────────────────────────────────────────────────────────────

def _row(verdict, name="g"):
    return {"name": name, "api_id": "1234", "verdict": verdict, "detail": "d"}


def test_summarize_exit_codes():
    assert summarize([_row("ok"), _row("ok")])[0] == 0
    code, text = summarize([_row("ok"), _row("bad", name="dead-grp")])
    assert code == 1 and "dead-grp" in text, "门禁语义：有 bad 必须 exit 1 且点名坏组"
    # 全部探不通 = 探针环境坏了，看门狗自身死亡也必须响（exit 2）
    assert summarize([_row("unreachable"), _row("unreachable")])[0] == 2
    assert summarize([])[0] == 2
    # 限流不算失败也不算成功：仅限流时不能假装全绿
    assert summarize([_row("rate_limited")])[0] == 2
    # 有成功垫底时，限流/偶发不可达可容忍（下轮再判）
    assert summarize([_row("ok"), _row("rate_limited")])[0] == 0


# ── 代理参数解析 ──────────────────────────────────────────────────────────────

def test_parse_proxy_arg_variants():
    p = parse_proxy_arg("socks5://127.0.0.1:7890")
    assert p == {"scheme": "socks5", "hostname": "127.0.0.1", "port": 7890}
    # 裸 host:port 默认 socks5（客服口头指引最常见形态）
    assert parse_proxy_arg("127.0.0.1:7890")["scheme"] == "socks5"
    withauth = parse_proxy_arg("socks5://u:p%40ss@1.2.3.4:1080")
    assert withauth["username"] == "u" and withauth["password"] == "p@ss"
    assert parse_proxy_arg("") is None
    assert parse_proxy_arg("::::") is None


def test_mask_hash_never_leaks():
    assert mask_hash(_HASH) == "…cdef"
    assert _HASH[:-4] not in mask_hash(_HASH)
    assert mask_hash("ab") == "…"


def test_probe_module_importable_without_pyrogram_side_effects():
    """脚本顶层禁 import pyrogram（解析/分类在无 pyrogram 的 VPS 也要能跑）。

    用 AST 判**模块级** import（函数体内的 lazy import 合法；注释/文档里提到
    pyrogram 字样不算——首版子串匹配曾被自己的注释误伤）。
    """
    import ast

    import scripts.tg_cred_probe as mod
    with open(mod.__file__, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:  # 只看模块顶层，函数体内的 lazy import 不在此列
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(str(n).startswith("pyrogram") for n in names), \
            f"模块级 import pyrogram：{names}（会让无 pyrogram 的环境连解析都跑不了）"


def test_probe_cred_classifies_via_fake_client(monkeypatch):
    """probe_cred 的异常→verdict 通路（不联网：连接即抛事故原文）。"""
    import scripts.tg_cred_probe as mod
    try:
        import pyrogram
    except Exception:
        import pytest
        pytest.skip("pyrogram 未安装")

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def connect(self):
            raise Exception("[400 API_ID_INVALID] - invalid")  # noqa: TRY002

        async def disconnect(self):
            pass

    monkeypatch.setattr(pyrogram, "Client", _Boom)
    res = asyncio.run(mod.probe_cred("123456", _HASH, timeout=5))
    assert res["verdict"] == "bad"
    assert "API_ID_INVALID" in res["detail"]
