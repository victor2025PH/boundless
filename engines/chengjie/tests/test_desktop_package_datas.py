"""安装版打包完整性门禁（源码好、安装版坏 —— 这一类问题的静态防线）。

事故（0.2.0/0.2.1 实测，2026-07-27 在 173 复现）：
`admin.py` 用 ``Path(__file__).resolve().parents[2] / "shared" / "copilot"`` 定位
两端共享组件库并挂到 ``/copilot``，但 ``desktop/build/build_backend.py`` 的 ``DATAS``
里没有这一项 → 冻结后 ``<_MEIPASS>/shared/copilot`` 不存在 → ``if is_dir()`` 静默跳过
→ ``/copilot/*`` 全 404。表现是桌面右栏业务助手 iframe 显示 ``{"detail":"Not Found"}``，
网页工作台侧栏的 cp-* 组件与 tokens.css 一并失效。

源码部署永远看不到（目录就在仓库里），CI 也全跑在源码环境，于是缺陷一路溜进安装包。
本门禁把「后端按 __file__ 相对定位、且位于 src 包之外的资源目录」这条不变量固化下来：
新增这类资源却忘了进 DATAS，测试立刻红。

运行期验收另见 ``desktop/build/smoke_backend.py``（真起 exe 打 URL 矩阵）。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ENGINE_ROOT / "desktop" / "build" / "build_backend.py"
_WEB_DIR = _ENGINE_ROOT / "src" / "web"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("_bd_build_backend", _BUILD_SCRIPT)
    assert spec and spec.loader, f"无法加载打包脚本: {_BUILD_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def build_mod():
    if not _BUILD_SCRIPT.is_file():
        pytest.skip(f"打包脚本不存在: {_BUILD_SCRIPT}")
    return _load_build_module()


def _packaged_rel_dirs(mod) -> set[str]:
    """DATAS + 暂存目录覆盖到的「包内目标相对路径」集合。"""
    dests = {str(dst).replace("\\", "/").strip("/") for _src, dst in mod.DATAS}
    # 暂存清洗后再打的目录（static / domains）由 main() 追加，口径取自同一常量
    dests.update(str(d).replace("\\", "/").strip("/") for d in mod.STAGED_DESTS)
    return dests


def test_shared_copilot_is_packaged(build_mod):
    """/copilot 静态挂载的源目录必须随包，否则安装版右栏业务助手必 404。"""
    assert "shared/copilot" in _packaged_rel_dirs(build_mod), (
        "DATAS 缺 shared/copilot —— 冻结后 /copilot/* 全 404（业务助手 iframe 显示 "
        "{\"detail\":\"Not Found\"}）。修复：在 build_backend.py 的 DATAS 里加 "
        '(REPO / "shared" / "copilot", "shared/copilot")'
    )


def test_web_assets_are_packaged(build_mod):
    """模板与静态资源是后端出页面的底线依赖。"""
    packaged = _packaged_rel_dirs(build_mod)
    for rel in ("src/web/templates", "src/web/static"):
        assert rel in packaged, f"DATAS 缺 {rel}"


def test_platform_bridges_are_packaged(build_mod):
    """引擎按**文件路径**加载的 platform 瘦模块必须显式登记。

    它们没有 import 语句，PyInstaller 静态分析追不到——漏打不会报错，只会静默降级
    （中央凭据池失效 / 绑机校验放行）。credpool 在 0.2.1 安装版就是这样漏掉的。
    """
    packaged = _packaged_rel_dirs(build_mod)
    for rel in ("platform/credpool", "platform/licensing"):
        src = _ENGINE_ROOT.parents[1] / rel
        if not src.is_dir():
            continue                     # 该瘦模块尚未存在于本仓，跳过
        assert rel in packaged, (
            f"DATAS 缺 {rel} —— 冻结后按路径加载会找不到，能力静默降级且无报错"
        )


def test_domains_are_packaged(build_mod):
    """领域包随包：漏了不报错、只是 AI 静默丢掉领域提示词与看板挂件。"""
    assert "domains" in _packaged_rel_dirs(build_mod), (
        "DATAS 缺 domains —— 安装版日志会出 \"Domain 'xxx' has no manifest.yaml\"，"
        "领域系统提示词/术语/上下文补充/看板挂件全部静默失效。"
    )


def test_platform_pkgs_not_raw_in_datas(build_mod):
    """platform/credpool|licensing **绝不能**以整目录原样进 DATAS（必须走暂存清洗）。

    事故（2026-08-09 实锤）：整目录 --add-data 把 credpool/config/account_registry.db
    （真实 Telegram 账号 + credpool_cred）、credpool/config/registry.key（Fernet 解密钥）、
    credpool/data/tgmatrix.db 一并打进**公网安装包** = 账号 + 解密钥同包交付。这些数据文件
    运行时从不被读（消费方只按路径加载 .py），必须经 _stage_platform_pkg 清洗后再打。
    """
    platform_root = build_mod._PLATFORM_ROOT
    offenders = []
    for src, dst in build_mod.DATAS:
        src_p = Path(src).resolve()
        try:
            inside = src_p == platform_root.resolve() or platform_root.resolve() in src_p.parents
        except Exception:
            inside = False
        # 只有「原目录」才违规；暂存目录（platform-staged）不在 _PLATFORM_ROOT 下，放行
        if inside:
            offenders.append(f"{src} -> {dst}")
    assert not offenders, (
        "DATAS 直指 platform 原目录（会把 account_registry.db / registry.key / *.db "
        "打进公网安装包）：\n  " + "\n  ".join(offenders)
        + "\n改用 _stage_platform_pkg 暂存清洗后再打（见 _staged_datas）。"
    )


def test_platform_staging_strips_secrets(build_mod):
    """暂存 platform 瘦模块后：机密数据必被剔净、瘦客户端代码必须幸存。

    这是上一条门禁的运行期对照：不仅「不许原目录直打」，还要证明「清洗真的把
    .db/.key/.pem/.sqlite 全剔了」且没误伤 credpool_client.py 这类真正要随包的代码。
    """
    secret_suffixes = {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
                       ".key", ".pem"}
    for pkg in build_mod.PLATFORM_PKGS:
        src = build_mod._PLATFORM_ROOT / pkg
        if not src.is_dir():
            continue
        staged = build_mod._stage_platform_pkg(pkg)
        assert staged is not None and staged.is_dir()
        leaked = [
            str(p.relative_to(staged))
            for p in staged.rglob("*")
            if p.is_file() and p.suffix.lower() in secret_suffixes
        ]
        assert not leaked, f"platform/{pkg} 暂存仍含机密数据（会泄漏进安装包）: {leaked}"
    # credpool 瘦客户端是运行时按路径加载的唯一必需文件，清洗不得误删
    cred = build_mod._stage_platform_pkg("credpool")
    if cred is not None:
        assert (cred / "credpool_client.py").is_file(), (
            "清洗把 credpool_client.py 也剔掉了 —— 中央凭据池会静默失效"
        )


def test_datas_sources_exist(build_mod):
    """DATAS 里登记的源路径必须真实存在——写错路径时 PyInstaller 只打印一行
    「跳过不存在的数据」就继续，构建照样成功、缺陷照样出厂。"""
    missing = [str(src) for src, _dst in build_mod.DATAS if not Path(src).exists()]
    assert not missing, f"DATAS 指向不存在的源路径（会被静默跳过）: {missing}"


def test_copilot_mount_still_resolves_to_shared_copilot():
    """admin.py 的挂载源一旦改名/挪窝，本文件上面的断言就名存实亡 —— 这里钉住它。"""
    text = (_WEB_DIR / "admin.py").read_text(encoding="utf-8")
    assert 'parents[2] / "shared" / "copilot"' in text, (
        "admin.py 的 /copilot 挂载源表达式变了：请同步更新 build_backend.py 的 DATAS "
        "与本门禁的断言。"
    )


def test_out_of_package_web_assets_are_registered(build_mod):
    """棘轮：src/web 里凡以 parents[2]（= 仓库根）定位的资源目录，都必须在 DATAS 中。

    只扫 src/web —— 静态挂载与模板加载都在这里，是「打包漏资源」风险的集中区；
    扫全库会把大量运行期可写路径（日志/DB/媒体）当成打包资产，噪声压过信号。
    """
    packaged = _packaged_rel_dirs(build_mod)
    pattern = re.compile(r'parents\[2\]\s*((?:/\s*"[^"]+"\s*)+)')
    offenders: list[str] = []
    seen = 0
    for py in sorted(_WEB_DIR.rglob("*.py")):
        for tail in pattern.findall(py.read_text(encoding="utf-8")):
            segs = re.findall(r'"([^"]+)"', tail)
            if not segs:
                continue
            seen += 1
            rel = "/".join(segs)
            if rel not in packaged:
                offenders.append(f"{py.relative_to(_ENGINE_ROOT).as_posix()} -> {rel}")
    # 自证：扫不到任何候选说明表达式风格变了，棘轮已空转（比漏报更危险，因为它看起来是绿的）
    assert seen >= 1, (
        "src/web 里一个 parents[2] 资源引用都没扫到——要么写法变了、要么正则失效，"
        "本门禁已形同虚设，请修正扫描规则。"
    )
    assert not offenders, (
        "以下仓库根资源被 src/web 引用但未随包（安装版会静默失效）：\n  "
        + "\n  ".join(offenders)
    )
