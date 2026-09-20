"""桌面随包种子的「产品基线可见性」门禁（2026-07-31，P0）。

事故形态（演示机 vs 安装包差异的完整因果链，工作目标为实锤标本）：
功能代码 100% 随包（templates / shared/copilot / goal_routes 全在 PyInstaller
DATAS / collect-submodules 里），但功能开关走「新子系统默认关」约定，演示机靠
config.local.yaml overlay 打开——而 overlay 在 .gitignore 里、刻意不随包。叠加
「关闭 = API 403 = 前端整卡隐藏」（cp-goal.js _hideCard）且首启向导、能力面板
都没有开启入口，客户拿到的安装包里这些功能**等于不存在**，且无任何自助途径
发现/开启（唯一办法是手改用户数据区 YAML）。

本门禁与 test_desktop_seed_deliverable.py 互为镜像，合起来是同一条不变量的两半：
- deliverable：种子里**开了**的方式，交付物必须真在包里（防「开了但跑不动」）；
- visibility（本文件）：产品基线功能在种子里必须**开着**（防「在包里但看不见」），
  且 C 类（LAN 依赖 / 风险行为）功能绝不允许进种子——防止将来有人图省事把演示机
  overlay 整段拷进种子，把「开关开了但依赖没随包」这种更糟的形态带给客户。

三分法判据（决策台账见 docs/安装包功能交付分级_2026-07.md）：
- A 类＝纯软件：本地 DB + 随包 AI 链即可跑、无 LAN/GPU 依赖、无封号/合规风险
  → 进种子默认开；
- B 类＝有依赖但客户可显式配置解锁 → 默认关，走功能总览可发现（不在本门禁）；
- C 类＝绑本机集群 / 风险行为 / 待产品拍板 → 进种子即红。

分级表的**单一事实源在 ``src/utils/feature_registry.py``**（P1 起）：同一张表
同时驱动本门禁、桌面态基线补齐（ConfigManager._ensure_baseline）与
/api/setup/features 功能总览——改分级只改注册表，本文件自动跟随。

纯文件读取 + 自建 app，不需要先打包，CI 常驻。
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.utils.feature_registry import (
    dig as _dig,
    product_baseline_map,
    product_baseline_values,
    seed_forbidden_map,
)

ENGINE_ROOT = Path(__file__).resolve().parent.parent
SEED = ENGINE_ROOT / "config" / "config.desktop.min.yaml"

PRODUCT_BASELINE = product_baseline_map()   # A 类 {key: 人话理由}
MUST_STAY_OFF = seed_forbidden_map()        # C 类 {key: 人话理由}


def _seed_cfg() -> dict:
    cfg = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    assert isinstance(cfg, dict) and cfg, "种子必须是非空 YAML mapping"
    return cfg


# ── 声明表 ↔ 种子内容 ────────────────────────────────────────────────────────

def test_baseline_keys_enabled_in_seed():
    """A 类必须在种子里落**注册表声明的那个值**。

    早期这里写死 `is not True`，2026-08-20 起 baseline 出现非布尔档位值
    （`contacts.mode="lite"`）——按 True 校验会把「正确写了 lite」判成缺失，
    而按声明值校验同时抓住「写成 full」这种更危险的偏差。
    """
    cfg = _seed_cfg()
    want = product_baseline_values()
    missing = {k: (want[k], _dig(cfg, k)) for k in want
               if _dig(cfg, k) != want[k]}
    assert not missing, (
        "产品基线功能没在桌面种子里按声明值交付（客户装完会「功能不存在」或跑错档）：\n"
        + "\n".join(
            f"  {k}: 应为 {exp!r}，种子里是 {got!r} —— {PRODUCT_BASELINE[k]}"
            for k, (exp, got) in missing.items()))


def test_c_class_keys_absent_or_off():
    cfg = _seed_cfg()
    leaked = {k: _dig(cfg, k) for k in MUST_STAY_OFF if bool(_dig(cfg, k))}
    assert not leaked, (
        "C 类功能被打开进了种子（依赖/风险没随包，比隐藏更糟）：\n" + "\n".join(
            f"  {k}={v} —— {MUST_STAY_OFF[k]}" for k, v in leaked.items()))


def test_baseline_and_denylist_disjoint():
    """两表来自注册表同一张表的不同分级，天然互斥；空表=注册表被误清也要红。"""
    assert PRODUCT_BASELINE and MUST_STAY_OFF, "注册表 A/C 类不得为空"
    overlap = set(PRODUCT_BASELINE) & set(MUST_STAY_OFF)
    assert not overlap, f"分级冲突：{sorted(overlap)}"


# ── 出厂默认节奏「不秒回」不变量（2026-08-07：修「装完即秒回」出货缺陷）──────
# 背景：自动回复的投递延迟读 inbox.l2_autosend.deliver_delay。种子里若缺该块
# 或配成 0/0，出站恒 0s 秒回——客户与平台风控当场识破机器人，正是老板实锤的
# 「设了不生效、一直秒回」。这两条门禁钉住「出厂即拟人」：任何人把种子/示例的
# deliver_delay 改回 0 或删掉，CI 立刻红（比等客户装完投诉早无数个数量级）。
# 2026-08-22 起种子 deliver=true（全自动开箱，deliver C→B 拍板）——本条不再与
# 「deliver 关着」组合成立，而是升级成「出厂即真发 ⇒ 拟人节奏更是硬底线」。

_INSTANT_FLOOR_SEC = 3.0   # <3s＝秒回带（与 reply_settings 的 bot-band 同口径）
ENGINE_ROOT_CFG = ENGINE_ROOT / "config"


def _deliver_delay_of(path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (((cfg.get("inbox") or {}).get("l2_autosend") or {})
            .get("deliver_delay")) or {}


def test_desktop_seed_ships_human_like_pacing():
    dd = _deliver_delay_of(SEED)
    mx = float(dd.get("max_sec", 0) or 0)
    assert mx >= _INSTANT_FLOOR_SEC, (
        f"桌面种子 deliver_delay.max_sec={mx} < {_INSTANT_FLOOR_SEC}s——新装机"
        "协议链会秒回（装完即露馅）。种子应带保守拟人区间（如 8–20s）。")


def test_example_config_ships_human_like_pacing():
    example = ENGINE_ROOT_CFG / "config.example.yaml"
    if not example.exists():
        pytest.skip("no config.example.yaml")
    dd = _deliver_delay_of(example)
    mx = float(dd.get("max_sec", 0) or 0)
    assert mx >= _INSTANT_FLOOR_SEC, (
        f"示例配置 deliver_delay.max_sec={mx} < {_INSTANT_FLOOR_SEC}s——照抄示例"
        "部署的自建实例会秒回。示例应带非零拟人默认。")


# ── 路由级探针：种子配置起 app，工作目标真的可见 ─────────────────────────────
# 探针端点选 /api/goals/agenda——与前端 featureOn 判定（unified_inbox.html）
# 完全同口径：非 403 = 功能可见。

@pytest.fixture(autouse=True)
def _hermetic_goal_env(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc
    from src.companion.goals.store import reset_goal_store

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


def _client_with(cfg: dict, tmp_path: Path) -> TestClient:
    """种子 dict 起最小 app（config_path 指向 tmp——目标库随之落 tmp，
    镜像生产桌面态「db 落 AITR_CONFIG_PATH 同目录」的布局，绝不写仓库 config/）。"""
    from src.web.routes.goal_routes import register_goal_routes

    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = {"role": "", "user": "seed-probe"}

    register_goal_routes(
        app, auth_dep,
        SimpleNamespace(config=cfg, config_path=str(tmp_path / "config.yaml")))
    return TestClient(app)


def test_goal_feature_visible_under_seed_config(tmp_path):
    client = _client_with(_seed_cfg(), tmp_path)
    r = client.get("/api/goals/agenda")
    assert r.status_code == 200, f"种子配置下工作目标应可见，实际 {r.status_code}"
    assert r.json().get("ok") is True
    assert client.get("/api/goals/templates").status_code == 200


def test_probe_detects_disabled(tmp_path):
    """探测器有效性自证：把开关拨回 false，同一探针必须变 403——
    证明上面那条绿不是「探针探了个寂寞」。"""
    cfg = copy.deepcopy(_seed_cfg())
    cfg.setdefault("companion", {}).setdefault("goals", {})["enabled"] = False
    client = _client_with(cfg, tmp_path)
    assert client.get("/api/goals/agenda").status_code == 403
