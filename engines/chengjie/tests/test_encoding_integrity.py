# -*- coding: utf-8 -*-
"""前端源文件编码完整性门禁（2026-08-22「收件箱全站 500」事故的机制化沉淀）。

事故：14:22 某编辑会话把 ``unified_inbox.html`` 以错误编码链保存（UTF-8 字节被按
GB18030/CP936 读入再存出——PS5.1 ``Get-Content``/ANSI 默认读法是最常见向量），
全文中文变乱码、文件膨胀 +30%，且乱码恰好吃掉 Jinja 字符串的闭合引号 →
``/workspace`` 500，全部坐席宕机 ~28 分钟；同一会话还把桌面镜像
``desktop/renderer/shared/copilot/app.html`` 存成乱码（若未发现会随下次打包出货）。

模板热更新＝保存即生产，这类损毁没有任何「部署前」拦截点——只能靠常驻门禁把
「文件还是不是合法 UTF-8 中文源码」变成机器可查的不变量：

  1. **UTF-8 严格可解码**（``errors='strict'``）——半个多字节序列都不许；
  2. **零 U+FFFD**——替换符出现在源码里＝某次有损转码的化石；
  3. **乱码指纹**——「UTF-8 被按 GBK 系解码」的产物落在一小撮生僻字上
     （鐨=的、锛=，、銆=。、鈥=引号族…），正常中文技术文本里几乎绝不连续出现。
     判定阈值＝distinct ≥2 且 total ≥4（首轮全站校准：干净树全部 0 命中，
     事故件命中 200+；阈值留了 2 倍余量防单字生僻词误伤）。

维护：
  - 新增豁免必须进 ``_ACCEPTED``（附原因）；防过期测试会点名已不再命中的登记。
  - 探测器自证：把真实中文正向损毁（utf8→按 gb18030 解码）后必须被抓到。
  - 扫描范围＝会被热更新直接服务/打包出货的前端源码树；二进制/字体/图片按扩展名跳过。
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# 会被服务/打包的前端源码树（+ 中文最密集的 i18n packs）
_SCAN_ROOTS = [
    _ROOT / "src" / "web" / "templates",
    _ROOT / "src" / "web" / "static",
    _ROOT / "src" / "web" / "i18n_packs",
    _ROOT / "shared" / "copilot",
    _ROOT / "desktop" / "renderer",
]
# 刻意不含 .svg：PWA 图标等工具导出的 SVG 资产自带 XML 编码声明（实测 icon.svg 是
# 非 UTF-8 的二进制导出），不属「手写中文源码」防护面；扫它们只产噪音。
_TEXT_EXT = {".html", ".js", ".css", ".py", ".json", ".txt", ".md", ".yaml", ".yml"}

# 「UTF-8 按 GBK 系误读」的高置信指纹字（对应常用字/标点的乱码形态）。
# 单字都是极生僻汉字，正常中文技术文本不会成组出现——distinct≥2 且 total≥4 才判。
_MOJIBAKE_MARKS = ("鐨", "锛", "銆", "鈥", "涔", "鍦", "鎴", "娓", "宸ヤ", "锟斤拷")
_MIN_DISTINCT = 2
_MIN_TOTAL = 4

# 良性命中登记（("相对路径", "原因")；防过期测试守）。
_ACCEPTED: dict = {}


def _iter_files():
    for root in _SCAN_ROOTS:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in _TEXT_EXT:
                yield p


def _rel(p: Path) -> str:
    return p.relative_to(_ROOT).as_posix()


def classify(text: str) -> dict:
    """对文本做三层编码体检；返回 {fffd, marks_total, marks_distinct}。"""
    fffd = text.count("\ufffd")
    hits = {m: text.count(m) for m in _MOJIBAKE_MARKS}
    total = sum(hits.values())
    distinct = sum(1 for v in hits.values() if v > 0)
    return {"fffd": fffd, "marks_total": total, "marks_distinct": distinct}


def _is_mojibake(info: dict) -> bool:
    return info["marks_distinct"] >= _MIN_DISTINCT and info["marks_total"] >= _MIN_TOTAL


def test_all_frontend_sources_are_clean_utf8():
    bad_decode, has_fffd, mojibake = [], [], []
    for p in _iter_files():
        rel = _rel(p)
        if rel in _ACCEPTED:
            continue
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as e:
            bad_decode.append(f"{rel}: {str(e)[:80]}")
            continue
        info = classify(text)
        if info["fffd"]:
            has_fffd.append(f"{rel}: U+FFFD ×{info['fffd']}")
        if _is_mojibake(info):
            mojibake.append(
                f"{rel}: 乱码指纹 total={info['marks_total']} distinct={info['marks_distinct']}")
    problems = bad_decode + has_fffd + mojibake
    assert not problems, (
        "前端源文件编码损毁（2026-08-22 收件箱 500 事故同款——热更新即生产，"
        "保存前你的编辑器/管道把 UTF-8 按 GBK 读了）：\n  " + "\n  ".join(problems)
        + "\n处置：从 git/构建快照/编辑器历史找完好版本恢复；"
          "PS5.1 的 Get-Content/Set-Content 不带 -Encoding 是最常见损毁向量，"
          "文件拷贝一律用二进制语义（copy /Y、shutil.copy）。"
    )


def test_detector_catches_real_corruption():
    """自证：把真实中文按事故向量正向损毁，必须被抓到。"""
    healthy = (
        "// 收件箱主脚本：会话列表按最近活动排序，未读优先。\n"
        "const title = '聊天工作台';  // 顶栏标题，随 i18n 切换\n"
        "/* 需人工的会话置顶，红色徽标显示可行动数。 */\n"
    )
    corrupted = healthy.encode("utf-8").decode("gb18030", errors="replace")
    info_h = classify(healthy)
    info_c = classify(corrupted)
    assert not _is_mojibake(info_h), "健康中文被误判＝阈值过紧，会误伤全站"
    assert _is_mojibake(info_c) or info_c["fffd"] > 0, "事故同款损毁没被抓到＝门禁失效"


def test_accepted_not_stale():
    live = set()
    for p in _iter_files():
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            live.add(_rel(p))
            continue
        info = classify(text)
        if info["fffd"] or _is_mojibake(info):
            live.add(_rel(p))
    stale = [k for k in _ACCEPTED if k not in live]
    assert not stale, f"登记项已不再命中，请清理台账：{stale}"


# ═══════════════════ 后端 .py 树：棘轮档（2026-08-27 追加）═══════════════════
#
# 上面那半边是**前端零容忍**（热更新即生产，一个坏字节全站 500）。后端 .py 此前完全
# 没有覆盖，而当天 F821 未定义名门禁（tests/test_python_undefined_names.py）顺带证明了
# 后端同样会被咬：``skill_manager.py`` L3559 的编码损毁**吃掉了一整条赋值语句**
# （`sid = s["strategy_id"]` 被折进乱码注释）→ `sid` 永不绑定 → NameError 被外层
# except 记 DEBUG 吞掉 → J4 策略参数 auto_tune 整段静默死亡。
#
# ⚠ 后端这半边**必须是棘轮而非零容忍**：实测全树 1283 个 .py 里只有 1 个真损毁，
# 但那一个有 995 处（822×U+FFFD + 173×PUA、散在 133 行），且 HEAD/HEAD~5/HEAD~20
# 全都一样 ⇒ 是**很久以前就提交进历史**的存量，不是谁今天存坏的。修它＝需要原始中文
# 的考古还原，属独立立项；这里先把「不许变得更糟、不许有新文件加入」钉死。
#
# 为什么 F821 门禁抓不全这类损毁：它只看得见「吃掉的东西恰好留下一个未定义名」。
# 若损毁吃掉的是 if 条件、return、或字符串字面量的一部分，语法仍合法、名字仍有定义，
# F821 完全沉默——所以编码完整性必须自己成为一条独立不变量。
_BACKEND_ROOTS = [_ROOT / "src", _ROOT / "scripts", _ROOT / "tools"]
_BACKEND_SKIP_PARTS = frozenset({
    "__pycache__", "node_modules", ".git", ".venv", "venv", "site-packages",
    "build", "dist",
})

#: 存量损毁天花板（U+FFFD + PUA 合计，**只降不升**）。修好就把数字调下来。
_BACKEND_DAMAGE_CEILINGS = {
    "src/skills/skill_manager.py": 995,
}

#: 良性命中（非损毁）——附原因，由 not_stale 检查防过期。
_BACKEND_ACCEPTED = {
    "scripts/sync_copilot_mirror.py":
        "该脚本本身就是乱码探测器，源码里字面声明 _MOJIBAKE_MARKS 指纹字元组 —— "
        "指纹字出现在这里是它的职责，不是损毁。",
}


def _iter_backend_py():
    """后端 .py，去重并剔除上半边已零容忍覆盖的前端树。"""
    covered = [r.resolve() for r in _SCAN_ROOTS]
    seen = set()
    for root in _BACKEND_ROOTS:
        if not root.is_dir():
            continue
        for p in root.rglob("*.py"):
            if _BACKEND_SKIP_PARTS & set(p.parts):
                continue
            rp = p.resolve()
            if rp in seen or any(str(rp).startswith(str(c)) for c in covered):
                continue
            seen.add(rp)
            yield p


def _backend_damage(text: str) -> int:
    """损毁计量＝替换符 + PUA 私用区字符（两者都是有损转码留下的化石）。"""
    return text.count("\ufffd") + sum(1 for c in text if 0xE000 <= ord(c) <= 0xF8FF)


def test_backend_python_no_new_encoding_damage():
    """后端 .py 不许出现新的编码损毁；存量只降不升。"""
    newly, worse, undecodable = [], [], []
    for p in _iter_backend_py():
        rel = _rel(p)
        if rel in _BACKEND_ACCEPTED:
            continue
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as e:
            undecodable.append(f"{rel}: {str(e)[:80]}")
            continue
        dmg = _backend_damage(text)
        mojibake = _is_mojibake(classify(text))
        if rel in _BACKEND_DAMAGE_CEILINGS:
            cap = _BACKEND_DAMAGE_CEILINGS[rel]
            if dmg > cap:
                worse.append(f"{rel}: 损毁 {dmg} > 天花板 {cap}")
        elif dmg or mojibake:
            newly.append(f"{rel}: 损毁 {dmg}" + ("（含乱码指纹）" if mojibake else ""))

    problems = undecodable + newly + worse
    assert not problems, (
        "后端 .py 编码损毁（有损转码的化石：U+FFFD / PUA 私用区 / GBK 系乱码指纹）：\n  "
        + "\n  ".join(problems)
        + "\n\n为什么这要红：2026-08-27 实锤——skill_manager.py 的同类损毁**吃掉了一整条"
          "赋值语句**，语法仍合法，只是那个功能从此静默死亡。"
        + "\n处置：从 git 历史/编辑器历史找完好版本恢复；文件拷贝一律用二进制语义"
          "（copy /Y、shutil.copy），别用不带 -Encoding 的 PS Get-Content/Set-Content。"
        + "\n确属良性（如探测器自身声明指纹字）→ 登记进 _BACKEND_ACCEPTED 并写清原因。"
    )


def test_backend_ceilings_not_stale():
    """天花板不得虚高/指向已消失的文件——否则棘轮会悄悄退化成永久放行。

    留了 20% 松弛：修掉几个字符不该逼着改表，但大幅清理后必须收紧。
    """
    stale = []
    for rel, cap in _BACKEND_DAMAGE_CEILINGS.items():
        p = _ROOT / rel
        if not p.exists():
            stale.append(f"{rel}: 文件已不存在，请删除该条目")
            continue
        dmg = _backend_damage(p.read_text(encoding="utf-8", errors="replace"))
        if dmg == 0:
            stale.append(f"{rel}: 已完全修复（损毁 0），请删除该条目")
        elif dmg < cap * 0.8:
            stale.append(f"{rel}: 实际 {dmg} 已远低于天花板 {cap}，请收紧到 {dmg}")
    assert not stale, "存量天花板需要维护：\n  " + "\n  ".join(stale)


def test_backend_scan_actually_reaches_the_tree():
    """自证没有空跑：后端树应扫到上千个 .py，且已知的那个损毁件确实在扫描面内。"""
    rels = {_rel(p) for p in _iter_backend_py()}
    assert len(rels) > 1000, f"只扫到 {len(rels)} 个后端 .py，疑似 roots/过滤写错"
    assert "src/skills/skill_manager.py" in rels, "已知损毁件不在扫描面内＝门禁形同虚设"
    assert not any(r.startswith("src/web/templates/") for r in rels), \
        "前端树应由上半边零容忍覆盖，不该在后端棘轮里重复计入"
