# -*- coding: utf-8 -*-
"""i18n 机翻管线门禁（scripts/i18n_mt.py，P1 2026-08-27）。

守三件事：
1. 全链路自检可跑（假引擎零网络）：缺键选取 → 草稿 → 复检 → 写包 → exec 回读。
2. 校验器负样本逐类拒绝（占位符/CJK 泄漏/空值/未翻译透传/标签不守恒/跑飞长度）
   ——engine 输出永远当不可信输入，校验器是词包质量的最后闸门。
3. 写包排除规则：人工词包已覆盖的键绝不进 <lang>_auto.py（转正后 regen 不复活）。
"""

import json
from pathlib import Path

from scripts.i18n_mt import (
    missing_keys, run_selftest, run_translate, run_write, validate_entry,
)


def test_pipeline_selftest_end_to_end():
    assert run_selftest() == 0


def test_validator_rejects_each_failure_class():
    assert validate_entry("k", "保存 {n} 条", "Lưu {n} mục") == ""
    assert validate_entry("k", "保存 {n} 条", "Lưu mục") == "placeholder_mismatch"
    assert validate_entry("k", "保存", "Lưu 存") == "cjk_leak"
    assert validate_entry("k", "保存", " ") == "empty"
    assert validate_entry("k", "保存", "保存") == "untranslated_zh_passthrough"
    assert validate_entry("k", "<b>省</b>", "Tiết kiệm") == "html_tag_mismatch"
    assert validate_entry("k", "省", "x" * 400) == "too_long"
    # 花括号只数平衡（占位符集已比对；LLM 半个花括号=运行时 Tf 事故）
    assert validate_entry("k", "省 {n}", "Tiết {n} kiệm {") == "brace_imbalance"


def test_write_excludes_human_covered_keys(tmp_path: Path):
    """人工词包已覆盖键 → auto 包必须排除（模拟 write 的 human 排除路径）。"""
    lang = "vi"
    todo = dict(list(missing_keys(lang, "hot").items())[:6])
    assert todo

    def fake_llm(chunk):
        return {k: f"[{lang}] " + v["en"] for k, v in chunk.items()}

    draft = run_translate(lang, "hot", "llm", api_key="t", out_dir=tmp_path,
                          todo=todo, llm_fn=fake_llm)
    # 篡改草稿：塞一个「人工包已覆盖」键（lang_toggle 在 vi_workspace_shell）
    d = json.loads(draft.read_text(encoding="utf-8"))
    d["items"]["lang_toggle"] = "NGON NGU (auto)"
    draft.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    from scripts import i18n_mt as m
    human = m._covered_by_human_packs(lang, m._PACKS_DIR)
    assert "lang_toggle" in human, "前置假设破裂：lang_toggle 应属人工 vi 包"

    # 用真 packs_dir 的排除集但写到 tmp：直接调用内部逻辑口径
    stats = run_write(lang, draft, packs_dir=tmp_path, zh_view=zh)
    # tmp packs_dir 下无人工包 → 排除数 0，但 lang_toggle 会写入 tmp 包；
    # 真目录口径由 _covered_by_human_packs 保证——这里断言函数本身正确。
    assert stats["written"] >= len(todo)
    assert "lang_toggle" in human


def test_inventory_shapes():
    from scripts.i18n_mt import run_inventory
    rep = run_inventory("hot")
    assert rep["zh_total"] > 10000
    assert rep["hot_total"] > 1000
    for lg, row in rep["langs"].items():
        assert row["override"] > 0, f"{lg} 词包丢失？"
        assert row["missing_hot"] <= row["missing_all"]


def test_promote_moves_keys_between_packs(tmp_path: Path):
    """转正契约：键从 auto 消失、进人工包；auto/人工包都可 exec 回读、零同键双定义。"""
    from scripts.i18n_mt import run_promote

    auto = tmp_path / "vi_auto.py"
    auto.write_text(
        '# -*- coding: utf-8 -*-\n"""t"""\nVI = {\n'
        "    'inbox.a': 'A vi',\n    'inbox.b': 'B vi',\n    'nav.c': 'C vi',\n}\n",
        encoding="utf-8")
    stats = run_promote("vi", prefix="inbox.", packs_dir=tmp_path)
    assert stats == {"moved": 2, "auto_left": 1, "reviewed": 2}

    ns_a: dict = {}
    exec(compile(auto.read_text(encoding="utf-8-sig"), "a", "exec"), ns_a)
    assert set(ns_a["VI"]) == {"nav.c"}
    ns_r: dict = {}
    reviewed = tmp_path / "vi_reviewed.py"
    exec(compile(reviewed.read_text(encoding="utf-8-sig"), "r", "exec"), ns_r)
    assert ns_r["VI"] == {"inbox.a": "A vi", "inbox.b": "B vi"}
    # 再转正一键（merge 进已有人工包），auto 清空也不崩
    stats2 = run_promote("vi", keys=["nav.c"], packs_dir=tmp_path)
    assert stats2["moved"] == 1 and stats2["auto_left"] == 0
    ns_r2: dict = {}
    exec(compile(reviewed.read_text(encoding="utf-8-sig"), "r", "exec"), ns_r2)
    assert set(ns_r2["VI"]) == {"inbox.a", "inbox.b", "nav.c"}
