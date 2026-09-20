"""通讯录面板（联系人 Tab）的静态接线门禁——钉住三条「踩过才知道」的不变量。

面板本体是 Jinja 模板里的原生 JS，没有 JS 测试栈可跑单测；但下面三条都属于
「改错了不会报错、只会在生产上慢慢烧」的性质，故按本仓 ``test_*_wiring.py``
惯例做源码级扫描，把结论固化成门禁：

1. **未开口联系人不回源头像**（生产安全，优先级最高）。头像端点在磁盘未命中时会
   打一次真 RPC（Telegram 走 ``get_chat``、WhatsApp 走 Baileys profilePictureUrl），
   而「从没聊过的人」**必然**没有磁盘缓存。几千人的名单滚一遍＝几千次 RPC 排在生产
   client 的 loop 上，既可能触发 FloodWait 又会挤占真实消息收发——与「立即同步」限死
   10 分钟一次防的是同一件事（见 ``_CONTACTS_REFRESH_COOLDOWN_SEC`` 的注释）。
   若哪天有人把 ``<img>`` 改回无条件渲染，线上不会报错，只会在某个高峰把收发拖垮。
2. **分块渲染**：名单可达数千条，一次性 innerHTML 全量既拖首屏、又让 ``loading=lazy``
   的头像在快速滚动时瞬间并发一大把（把第 1 条的护栏冲掉）。
3. **未开口联系人乐观开面板**：``loadChats`` 只有 in-flight 去重、没有 TTL 缓存，且
   会整体替换 ``allChats``。未开口的人库里连会话行都没有，先刷一次 100 条全量纯属白等
   ——而「点未开口的人去主动开口」正是整个通讯录功能的主操作。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "unified_inbox.html"


@pytest.fixture(scope="module")
def html() -> str:
    return _TPL.read_text(encoding="utf-8")


def _contact_row_fn(html: str) -> str:
    """截出 ``_contactRowHtml`` 函数体（到下一个顶层 ``function`` 声明为止）。

    刻意不做括号配平：这里只需要一个「足够窄且稳定」的检查窗口，用下一个函数声明
    作边界既简单又不会因为函数内部的模板字面量/正则把配平器带偏。
    """
    start = html.index("function _contactRowHtml(")
    nxt = html.index("\nfunction ", start + 1)
    return html[start:nxt]


def test_avatar_not_fetched_for_never_spoke(html: str):
    """未开口联系人**不得**渲染 <img> 头像（防几千次 RPC 打爆生产 client）。"""
    body = _contact_row_fn(html)
    assert "/avatar?chat_key=" in body, "联系人行不再引用头像端点？该门禁需随实现更新"
    # <img> 必须落在以 never 为条件的三元里；拿到 <img> 之前必须先出现该条件
    cond = re.search(r"const\s+ava\s*=\s*never\s*\?", body)
    assert cond, (
        "未开口联系人的头像闸门丢失：`const ava=never ? '' : '<img…>'` 不见了。"
        "无条件渲染 <img> 会让「从没聊过的人」逐个触发真 RPC（必然磁盘未命中），"
        "几千条名单滚一遍即可触发 FloodWait 并挤占真实消息收发。"
    )
    assert cond.start() < body.index("/avatar?chat_key="), "头像闸门必须在 <img> 之前"


def test_contact_list_renders_in_chunks(html: str):
    """分块渲染在位：首屏只出一块，滚到底再续（同时天然限流并发头像请求）。"""
    assert "const _CONTACT_CHUNK=" in html, "分块大小常量丢失"
    assert "function _contactsAppendChunk(" in html, "续块函数丢失"
    assert "insertAdjacentHTML('beforeend'" in html, (
        "续块必须用 insertAdjacentHTML 追加；改回整体 innerHTML 赋值会退回"
        "「一次性渲染数千行」的老问题"
    )


def test_never_spoke_opens_optimistically(html: str):
    """未开口联系人走乐观开面板：调用方打 optimistic，深链入口认这个标记。"""
    assert "optimistic:!!never" in html, (
        "_contactStartChat 不再传 optimistic：未开口联系人会退回「先等一次 100 条"
        "全量拉取再开面板」，而这正是通讯录的主操作路径"
    )
    assert "if(p.optimistic){" in html, "__desktopOpenConversation 的乐观分支丢失"
    # 乐观分支必须补回被 loadChats 冲掉的占位，否则刚打开的会话会从列表消失
    assert "if(!allChats.find(x=>convKey(x)===key)) allChats.push(synth);" in html, (
        "乐观分支缺少对账补回：loadChats 会整体替换 allChats，占位被冲掉后"
        "刚打开的会话会从列表里消失"
    )


def test_truncation_is_disclosed_bilingually():
    """名单被单页上限截断时如实告知——「搜不到就以为没这个人」正是本轮要修的信任问题。"""
    from src.web.i18n_packs import inbox_workspace as pack

    key = "inbox.contacts.count_capped"
    assert key in pack.ZH and key in pack.EN, f"{key} 需 zh+en 双语齐备"
    assert "{n}" in pack.ZH[key] and "{n}" in pack.EN[key], "计数占位符缺失"


def test_proactive_skips_placeholder_conversations():
    """会话占位不得成为主动触达候选（本条与通讯录同源：占位就是目录同步造出来的）。

    目录同步把云端会话列表建成占位会话，让工作台贴近手机所见——它们带着手机上的真实
    ``last_ts``（可能沉默数月）却从未经本系统交谈过。主动触达按沉默时长降序挑人，不拦
    就会把这批陌生会话当成「好久不见的老朋友」挨个问候：既是没授权的外呼，也重演过
    ``PEER_ID_INVALID → ban_signal → 冻结主账号 1h`` 的事故路径。

    该护栏在 ``_conversations()`` 闭包里（需要整个 assistant 接线才能构造，不适合单测），
    故此处做源码级断言——这条不变量的代价太高，不能只靠注释守。
    """
    src = (Path(__file__).resolve().parents[1] / "src" / "companion"
           / "proactive_topic.py").read_text(encoding="utf-8")
    assert 'if not dirs_ok or not (dirs.get(cid) or {}).get("direction"):' in src, (
        "主动触达的「必须真交谈过」护栏丢失：会话占位（无消息 → 末条方向为空）会被"
        "当成沉默已久的老会话挨个主动问候"
    )
    # 查不到末条方向时必须 fail-closed（漏一轮零代价，无法核实就外呼代价极高）
    assert "dirs_ok = False" in src, "末条方向查询失败时的 fail-closed 标记丢失"


def test_server_side_search_covers_beyond_first_page(html: str):
    """本地集合被截断时，搜索必须回落到服务端 q（SQL 侧覆盖全表）。"""
    assert "if(!_contactsTruncated) return;" in html, (
        "服务端搜索的触发条件丢失：本地只有一页时不打后端（省一次往返），"
        "但一旦截断就必须打，否则第 2001 个人永远搜不到"
    )
    assert "_contactsSearchSeq" in html, "乱序回包的 seq 令牌丢失"
