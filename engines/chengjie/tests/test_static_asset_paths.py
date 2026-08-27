"""被服务静态资产的落盘路径门禁（2026-07-29 实锤事故后加）。

事故：``account_self_profile._DEFAULT_AVATAR_DIR`` 原本是**相对路径**
``"src/web/static/persona_avatars"``，按**进程 CWD** 解析。双实例部署的进程 CWD 是
实例数据根 → 自身头像被写进 ``<数据根>/src/web/static/persona_avatars/``，而
``admin.py`` 挂载的静态目录是 ``Path(__file__).parent/"static"``（**代码根**）
→ 注册表里存的 ``/static/persona_avatars/xxx.jpg`` 永远 404，坐席看到裂图。
更糟的是有指纹去重（``avatar_needs_refresh``）：头像指纹不变就不再重下，**404 永久固化**。
迁移前 CWD 恰好是代码根，故障因此只在迁移后新登录的账号上出现（实测 LINE 账号裂图）。

不变量：**任何「写了之后要通过 /static 被访问」的目录，都必须是绝对路径且落在
web 服务真正挂载的那个静态目录下**——绝不能依赖进程 CWD。

同类先例：``protocol_bridge`` 的出站媒体目录早就用 ``Path(__file__)`` 推导，本门禁
把该做法固化成全站不变量。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ENGINE_ROOT = Path(__file__).resolve().parent.parent
#: admin.py 的挂载点：app.mount("/static", StaticFiles(Path(__file__).parent/"static"))
SERVED_STATIC_DIR = ENGINE_ROOT / "src" / "web" / "static"


def test_served_static_dir_matches_admin_mount():
    """先钉住本门禁的前提：admin.py 确实按 __file__ 挂载 src/web/static。"""
    admin = (ENGINE_ROOT / "src" / "web" / "admin.py").read_text(
        encoding="utf-8", errors="replace")
    assert '_static_dir = Path(__file__).parent / "static"' in admin, (
        "admin.py 的静态挂载方式变了 → 本文件的 SERVED_STATIC_DIR 前提失效，需同步更新")
    assert SERVED_STATIC_DIR.is_dir()


def test_self_profile_avatar_dir_is_absolute_and_served():
    """自身头像落盘目录必须绝对，且就是被 /static 服务的那个目录。"""
    from src.integrations.account_self_profile import (
        _AVATAR_URL_PREFIX, _DEFAULT_AVATAR_DIR,
    )
    p = Path(_DEFAULT_AVATAR_DIR)
    assert p.is_absolute(), (
        f"_DEFAULT_AVATAR_DIR 必须绝对（当前 {_DEFAULT_AVATAR_DIR!r}）——"
        "相对路径按进程 CWD 解析，双实例部署会写进实例数据根而永不被服务")
    expected = SERVED_STATIC_DIR / "persona_avatars"
    assert p.resolve() == expected.resolve(), (
        f"落盘目录 {p} 不是被服务的 {expected}——存进注册表的 URL 会 404")
    # URL 前缀与目录名必须对得上，否则「文件写对了、URL 还是错的」
    assert _AVATAR_URL_PREFIX.rstrip("/").endswith(expected.name)


def test_voice_live_persona_avatar_dir_is_absolute_and_served():
    """人设头像存在性判断同样不能用 CWD 相对路径（否则整列头像不显）。"""
    from src.web.routes.voice_live_routes import _PERSONA_AVATAR_DIR

    assert _PERSONA_AVATAR_DIR.is_absolute()
    assert (_PERSONA_AVATAR_DIR.resolve()
            == (SERVED_STATIC_DIR / "persona_avatars").resolve())


def test_no_cwd_relative_served_static_paths_in_src():
    """全站扫描：源码里不得再出现 CWD 相对的 "src/web/static/..." 字面量。

    这类字面量在开发机（CWD=引擎根）看起来一切正常，只在真实部署（CWD=数据根）才炸，
    是最难在本地复现的一类缺陷——故用静态门禁在提交期挡住。
    """
    pat = re.compile(r"""['"]src/web/static/""")
    offenders: list[str] = []
    for f in (ENGINE_ROOT / "src").rglob("*.py"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            # 只看代码部分：事故说明写在注释里就会提到这个字面量（本仓即如此），
            # 不剔注释会把「记录教训的注释」本身判成违规。粗切 '#' 足够——
            # 该模式只出现在路径字面量场景，不会有「字符串内含 #」的歧义。
            code = line.split("#", 1)[0]
            if pat.search(code):
                offenders.append(f"{f.relative_to(ENGINE_ROOT).as_posix()}:{i}")
    assert not offenders, (
        "源码出现 CWD 相对的静态路径字面量（部署时 CWD=实例数据根 → 写/读落空）：\n  "
        + "\n  ".join(offenders)
        + "\n改法：Path(__file__).resolve().parents[N] / 'web' / 'static' / ...")


def test_protocol_media_root_also_absolute():
    """协议媒体落地根：绝对路径 + 数据根契约（账号资产 P0，2026-08-19 迁移）。

    旧不变量是「必须在引擎 static 下」；媒体迁入实例数据根后分两档：
    - 有数据根契约（``AITR_DATA_DIR`` / ``AITR_CONFIG_PATH``；生产双实例、桌面壳、
      测试 conftest 皆有）→ 根必须= <数据根>/protocol_media，且**绝不落引擎树**
      （那是实例备份带不走、多实例混居的位置——正是迁移的全部动机）；
    - 无契约（裸开发机/旧单实例）→ 回落旧引擎 static 位置（行为向后兼容）。
    URL 命名空间（/static/protocol_media/...）由 admin.py 的 ProtocolMediaStatic
    专属挂载（新根优先、旧根兜底）保证不变。
    """
    import os

    from src.integrations.protocol_bridge import (
        legacy_protocol_media_root, protocol_media_root,
    )

    d = protocol_media_root()
    assert d.is_absolute()
    data_dir = (os.environ.get("AITR_DATA_DIR") or "").strip()
    assert data_dir, "conftest 应已把 AITR_DATA_DIR 指向进程级 tmp（测试隔离契约）"
    if not (os.environ.get("AITR_CONFIG_PATH") or "").strip():
        assert d == Path(data_dir).expanduser() / "protocol_media"
    # 契约档绝不落引擎树（落了＝备份又带不走了）
    assert str(SERVED_STATIC_DIR.resolve()) not in str(d.resolve())
    # 旧根仍钉在引擎 static 下（它是迁移源 + 兜底挂载的 fallback，位置不能漂）
    legacy = legacy_protocol_media_root()
    assert legacy.is_absolute()
    assert str(SERVED_STATIC_DIR.resolve()) in str(legacy.resolve())


# ── 唯一刻意保留的 CWD 相对路径：预渲染语音库（两侧契约必须成对存在）────────


def test_prerender_writer_resolves_per_data_root():
    """预渲染 CLI 必须**显式**按数据根拼 base_dir，不能退回吃 CWD 的默认值。

    读取方（引擎进程）用 ``DEFAULT_BASE_DIR`` 相对路径、靠「引擎 CWD == 实例数据根」
    的启动契约与写入方同址（实测 112 个 clip 全在数据根）。这份「一侧显式、一侧靠
    CWD」的搭配是刻意的（见 voice_prerender.DEFAULT_BASE_DIR 注释），但**前提是写入
    方保持显式**：若 CLI 哪天退回默认值，它会按夜间脚本的 CWD（``Set-Location``
    引擎根）写进引擎根，而引擎仍读数据根 → 备货全体失效、只表现为语音变慢。
    """
    cli = (ENGINE_ROOT / "scripts" / "avatar_prerender.py").read_text(
        encoding="utf-8", errors="replace")
    assert "resolve_data_roots" in cli, "CLI 必须按数据根契约解析"
    assert 'base_dir = args.base_dir or str(Path(root) / DEFAULT_BASE_DIR)' in cli, (
        "CLI 的 base_dir 必须显式按数据根拼（当前写法变了 → 备货可能写进引擎根，"
        "引擎读数据根 → 预渲染命中静默归零）")


def test_no_unregistered_risky_relative_paths():
    """全站 AST 扫描：A/B 类（真缺陷类）相对路径必须**零未登记条目**。

    与上面那条正则扫描（只管 A 类的 ``"src/web/static/..."`` 字面量）互补：本条用
    ``tools/audit_relative_paths`` 的 AST 分类器，能抓到 B 类（templates/domains/
    shared/assets… 等**代码根资源**用相对路径 → 生产 CWD 是实例数据根 → 解析落空）。

    分类逻辑与那个工具**同一事实源**（避免「工具报绿、门禁口径不同」的两套真相）。
    新增刻意例外要同时改 ``tools/audit_relative_paths._INTENTIONAL`` 并在该处写清
    「为什么刻意如此 + 失效如何被观测到」——参考 voice_prerender 那处的成对门禁。
    """
    from tools.audit_relative_paths import risky_unregistered

    offenders = risky_unregistered()
    assert not offenders, (
        "出现未登记的高风险 CWD 相对路径（生产 CWD=实例数据根，会解析落空/写了不被服务）：\n  "
        + "\n  ".join(offenders)
        + "\n改法：Path(__file__).resolve().parents[N] / ... 推绝对路径；"
          "确属刻意例外则登记进 tools/audit_relative_paths._INTENTIONAL 并写清理由。"
        + "\n自查：python tools/audit_relative_paths.py --strict")


def test_audit_classifier_actually_discriminates():
    """探测器自证：分类器必须真能区分三类，否则「零违规」是假绿。"""
    from tools.audit_relative_paths import classify

    assert classify("x.py", "src/web/static/persona_avatars") == "A_SERVED_STATIC"
    assert classify("x.py", "templates/foo.html") == "B_CODE_ROOT"
    assert classify("x.py", "assets/voices") == "B_CODE_ROOT"
    assert classify("x.py", "logs/app.log") == "C_DATA_ok"
    assert classify("x.py", "config/foo.db") == "C_DATA_ok"


def test_prerender_relative_default_is_documented_as_intentional():
    """该相对路径必须带「为什么刻意如此 + 失效如何被观测到」的说明。

    没有这段说明，下一个人看到全站唯一的相对路径只会当成漏改的 bug 顺手「修」掉，
    而两侧要成对改才不出事。
    """
    src = (ENGINE_ROOT / "src" / "ai" / "voice_prerender.py").read_text(
        encoding="utf-8", errors="replace")
    head = src[:src.index("DEFAULT_BASE_DIR =")]
    for kw in ("resolve_data_roots", "prerender_coverage", "CWD"):
        assert kw in head, f"DEFAULT_BASE_DIR 上方说明缺少 {kw!r}"
