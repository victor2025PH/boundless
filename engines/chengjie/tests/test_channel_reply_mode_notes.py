"""渠道页「回复模式」语义说明行 + 收件箱封顶提示 静态门禁（常驻）。

背景（实录困惑）：系统三套并行自动化语义——① 坐席收件箱会话档位
（manual/review/multi_choice/auto_ai）；② LINE/Messenger/WhatsApp 渠道页的
RPA ``reply_mode``（auto/approve/off，设备链自己的开关）；③ AI 值守的
``inbox.l2_autosend.deliver`` 总闸。三者互不相干，运营在渠道页选「自动」
误以为等于收件箱全自动；另外 ``inbox.auto_draft.platform_modes`` 可把某平台
封顶为 review，收件箱顶栏有提示而渠道页原本不可见。

本门禁钉住三渠道页的补救 UI 不回退：

(a) reply_mode 控件正下方的语义说明行——三段 i18n key（note / note_link /
    note_tail）齐引用，且「聊天坐席」落在 ``<a href="/workspace">`` 链接上；
(b) 封顶提示——``#<pfx>-cap-note`` 元素（初始 display:none、每页唯一）+
    页内 JS 防御式读取壳层注入的 ``window.__chcPlatformCaps``（并行工作流
    在 workspace_channels.html 落地；本页读取自带 ``||{}`` 兜底不依赖时序）
    + ``window.Tf`` 参数化封顶文案；
(c) 三个 pack 新 key ZH/EN 齐平、cap 文案两语都含 ``{mode}`` 占位、
    key 全站唯一（仅定义于所属 pack，不与单体/其他 pack 冲突）。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.web.i18n_packs import line_page, messenger_page, whatsapp_page
from src.web.web_i18n import get_translations

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "src" / "web" / "templates"
_PACKS_DIR = _ROOT / "src" / "web" / "i18n_packs"

# 平台 → (模板文件名, DOM id 前缀, i18n key 前缀, pack 模块)
_PAGES = {
    "line": ("_channel_body_line.html", "lr", "lr", line_page),
    "messenger": ("_channel_body_messenger.html", "mr", "msg", messenger_page),
    "whatsapp": ("_channel_body_whatsapp.html", "wa", "wa", whatsapp_page),
}

_KEY_SUFFIXES = ("reply_mode_note", "reply_mode_note_link",
                 "reply_mode_note_tail", "reply_mode_cap")


def _keys_for(key_prefix: str) -> list:
    return [f"{key_prefix}_{s}" for s in _KEY_SUFFIXES]


def _read(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


# ── (a) 语义说明行：三段 key 引用 + /workspace 链接 ───────────────────────────

def test_note_line_keys_referenced_with_workspace_link():
    problems = []
    for platform, (tpl, _pfx, kpfx, _pack) in _PAGES.items():
        text = _read(tpl)
        for suffix in ("reply_mode_note", "reply_mode_note_link",
                       "reply_mode_note_tail"):
            key = f"{kpfx}_{suffix}"
            if f"'{key}'" not in text:
                problems.append(f"{tpl}: 缺少说明行 key 引用 {key!r}")
        # 「聊天坐席」链接：note_link key 必须渲染在 <a href="/workspace"> 内
        # （[^<] 而非 [^}]：Jinja 表达式里 `(i18n or {})` 自带 `}`）
        link_re = re.compile(
            r"<a\s+href=\"/workspace\">\{\{[^<]*'" + re.escape(kpfx)
            + r"_reply_mode_note_link'")
        if not link_re.search(text):
            problems.append(
                f"{tpl}: {kpfx}_reply_mode_note_link 未落在 "
                f"<a href=\"/workspace\"> 链接上")
    assert not problems, "语义说明行回退:\n" + "\n".join(problems)


# ── (b) 封顶提示：元素 + caps 防御式读取 + Tf 参数化 ──────────────────────────

def test_cap_note_element_present_hidden_and_unique():
    problems = []
    for platform, (tpl, pfx, _kpfx, _pack) in _PAGES.items():
        text = _read(tpl)
        elem_id = f"{pfx}-cap-note"
        tags = re.findall(
            r"<div\b[^>]*\bid=\"" + re.escape(elem_id) + r"\"[^>]*>", text)
        if len(tags) != 1:
            problems.append(f"{tpl}: id={elem_id!r} 出现 {len(tags)} 次（应恰 1 次）")
            continue
        if "display:none" not in tags[0]:
            problems.append(f"{tpl}: #{elem_id} 初始必须 display:none（JS 有值才显示）")
    assert not problems, "封顶提示元素回退:\n" + "\n".join(problems)


def test_cap_note_js_reads_caps_defensively_and_uses_tf():
    problems = []
    for platform, (tpl, pfx, kpfx, _pack) in _PAGES.items():
        text = _read(tpl)
        # 防御式读取：壳层变量可能尚未注入 / 为空对象
        read_expr = f"(window.__chcPlatformCaps||{{}})['{platform}']"
        if read_expr not in text:
            problems.append(f"{tpl}: 缺少防御式封顶读取 {read_expr}")
        tf_call = f"window.Tf('{kpfx}_reply_mode_cap'"
        if tf_call not in text:
            problems.append(f"{tpl}: 封顶文案未经 {tf_call}…) 参数化渲染")
        if f"getElementById('{pfx}-cap-note')" not in text:
            problems.append(f"{tpl}: JS 未定位 #{pfx}-cap-note 元素")
    assert not problems, "封顶提示 JS 回退:\n" + "\n".join(problems)


# ── (c) pack 键：双语齐平 + {mode} 占位 + 全站唯一 ────────────────────────────

def test_pack_keys_bilingual_and_cap_parameterized():
    problems = []
    for platform, (_tpl, _pfx, kpfx, pack) in _PAGES.items():
        for key in _keys_for(kpfx):
            for lang_name, table in (("ZH", pack.ZH), ("EN", pack.EN)):
                val = table.get(key)
                if not isinstance(val, str) or not val:
                    problems.append(
                        f"{pack.__name__}.{lang_name} 缺 key {key!r} 或值为空")
                elif key.endswith("reply_mode_cap") and "{mode}" not in val:
                    problems.append(
                        f"{pack.__name__}.{lang_name}[{key!r}] 缺 {{mode}} 占位")
    assert not problems, "pack 键双语回退:\n" + "\n".join(problems)


def test_keys_unique_across_all_i18n_sources():
    """每个新 key 仅在所属 pack 定义（ZH+EN 各一次），单体与其他 pack 零命中。

    防两类冲突：跨 pack 复制粘贴撞键（加载即抛错但要指名道姓）、
    与 web_i18n.py 单体既有键重名（双源真相）。
    """
    monolith = (_ROOT / "src" / "web" / "web_i18n.py").read_text(encoding="utf-8")
    pack_texts = {
        p.name: p.read_text(encoding="utf-8")
        for p in _PACKS_DIR.glob("*.py") if p.name != "__init__.py"
    }
    problems = []
    for platform, (_tpl, _pfx, kpfx, pack) in _PAGES.items():
        own_pack = Path(pack.__file__).name
        for key in _keys_for(kpfx):
            needle = f'"{key}"'
            if needle in monolith:
                problems.append(f"{key!r} 与 web_i18n.py 单体键重名")
            for pack_name, ptext in pack_texts.items():
                n = ptext.count(needle)
                if pack_name == own_pack:
                    if n != 2:  # ZH + EN 恰各一次
                        problems.append(
                            f"{key!r} 在 {pack_name} 出现 {n} 次（应恰 2 次=ZH+EN）")
                elif n:
                    problems.append(f"{key!r} 泄漏到其他 pack {pack_name}")
    assert not problems, "key 唯一性回退:\n" + "\n".join(problems)


def test_keys_resolve_in_merged_view():
    """合并视图（单体+packs）里新 key 必须解析为所属 pack 的值——
    证明加载链没把它们丢掉/覆盖（window.T 与 tr() 的真实取值路径）。"""
    zh, en = get_translations("zh"), get_translations("en")
    problems = []
    for platform, (_tpl, _pfx, kpfx, pack) in _PAGES.items():
        for key in _keys_for(kpfx):
            if zh.get(key) != pack.ZH[key]:
                problems.append(f"zh 合并视图 {key!r} != {pack.__name__}.ZH 值")
            if en.get(key) != pack.EN[key]:
                problems.append(f"en 合并视图 {key!r} != {pack.__name__}.EN 值")
    assert not problems, "合并视图回退:\n" + "\n".join(problems)
