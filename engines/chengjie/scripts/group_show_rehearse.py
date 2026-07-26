"""群戏排练 CLI —— 离线演一场，看剧本效果，**一条真消息都不发**。

用法
----
最快看一眼（占位台词，秒出，只验编排结构）::

    python -m scripts.group_show_rehearse --playbook growth_matrixx

看真实语感（走 LLM，慢，几十秒到几分钟）::

    python -m scripts.group_show_rehearse --playbook growth_matrixx --llm

带人设声音（每个号说话是自己的味道，Phase 2 新增）::

    python -m scripts.group_show_rehearse --playbook growth_matrixx --llm --persona

模拟真人插话，验让路与响应式接话::

    python -m scripts.group_show_rehearse --playbook growth_matrixx \\
        --human "2:老王:这玩意儿多少钱啊" --human "3:老王:有没有便宜点的"

为什么排练默认**不**放宽指纹约束
--------------------------------
排练是零风险的（不发消息），照理可以让所有号都上台把剧本演完整。但那样排出来的戏会
**骗人**：排练里四个人有来有回，真发时因为这些号共享同一个网络出口，实际只有一个人能
上台——排练与生产不同口径，是最典型的「演练全过、上线全崩」。

所以默认口径与真发完全一致：先跑关联体检，把「你最多只能有 N 个号同台」这个硬事实摆在
最前面。想在明知约束的前提下预览完整剧本，用 ``--allow-shared-host`` 显式放宽（这个开关
只影响排练，真发路径没有对应入口）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 允许 `python scripts/group_show_rehearse.py` 直接跑（不经 -m）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.companion.group_show.casting import HEALTH_OK  # noqa: E402
from src.companion.group_show.linkage import (  # noqa: E402
    derive_fingerprint_groups,
    format_readiness,
    linkage_readiness,
)
from src.companion.group_show.playbook import (  # noqa: E402
    Playbook,
    load_playbook_dir,
    validate_playbook,
)
from src.companion.group_show.runtime import (  # noqa: E402
    format_rehearsal,
    llm_generator,
    persona_line_generator,
    rehearse,
    stub_generator,
)

logger = logging.getLogger("group_show.rehearse")

#: 剧本库缺省目录。
PLAYBOOK_DIR = Path("config/playbooks")

#: 没有真实账号时用的占位演员数。四个刚好填满标准四角。
DEFAULT_STUB_ACTORS = 4


# ── 演员 ────────────────────────────────────────────────────────────────────


def stub_actors(count: int = DEFAULT_STUB_ACTORS) -> List[Dict[str, Any]]:
    """占位演员：结构与真实账号一致，但每个号自带独立指纹组。

    自带指纹是刻意的——占位演员是**虚构**的号，声明它们分属不同出口不算撒谎，
    这样「没有真号可用」的人也能把剧本结构完整排一遍。真号走 :func:`real_actors`，
    指纹由注册表说了算。
    """
    return [
        {
            "account_id": f"stub{i + 1}",
            "platform": "telegram",
            "persona_id": f"persona_{i + 1}",
            "display_name": f"演员{i + 1}",
            "health": HEALTH_OK,
            "fingerprint_group": f"stub-proxy-{i + 1}",
        }
        for i in range(max(1, count))
    ]


def real_actors(
    platform: str = "telegram",
    *,
    db_path: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """从账号注册表读真实在线号，返回 ``(选角候选, 注册表原始行)``。

    两份都要：候选喂给选角，原始行喂给关联体检（体检需要 ``proxy_id`` / ``mode`` /
    ``meta`` 这些候选结构里没有的字段）。读不到就返回两个空列表，由调用方回落占位。
    """
    try:
        from src.integrations.account_registry import get_account_registry
        registry = get_account_registry(Path(db_path) if db_path else None)
        rows = registry.list(platform=platform)
    except Exception as exc:  # noqa: BLE001 —— 没有注册表是常态（新机/纯离线）
        logger.debug("[rehearse] 读账号注册表失败，回落占位演员: %s", exc)
        return [], []

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        candidates.append({
            "account_id": str(row.get("account_id") or ""),
            "platform": str(row.get("platform") or platform),
            "persona_id": str(meta.get("persona_id") or ""),
            "display_name": str(meta.get("self_name")
                                or row.get("label") or ""),
            "health": "online" if str(row.get("status")) == "online" else "offline",
        })
    return candidates, rows


# ── 生成器 ──────────────────────────────────────────────────────────────────


async def build_ai_client() -> Any:
    """构造真 AI 客户端。

    两个必须 ``await`` 的坑（都踩过）：``ConfigManager.load()`` 是协程，漏 await 会让
    配置全空；``AIClient.initialize()`` 也是协程，漏 await 会缺 ``_tiers_enabled``
    之类的实例属性，报错还很不直观。
    """
    from src.ai.ai_client import AIClient
    from src.utils.config_manager import ConfigManager

    cfg_mgr = ConfigManager()
    await cfg_mgr.load()
    client = AIClient(cfg_mgr)
    await client.initialize()
    return client


# ── 真人插话 ────────────────────────────────────────────────────────────────


def parse_human(specs: Optional[Sequence[str]]) -> List[Tuple[int, str, str]]:
    """``"2:老王:多少钱"`` → ``(2, "老王", "多少钱")``。

    只按前两个冒号切，台词里的冒号原样保留（"老王:他说:好吧" 是合法输入）。
    """
    out: List[Tuple[int, str, str]] = []
    for spec in specs or []:
        parts = str(spec).split(":", 2)
        if len(parts) < 3:
            print(f"⚠ 跳过格式不对的 --human（应为 位置:谁:说什么）：{spec}")
            continue
        try:
            after = int(parts[0].strip())
        except ValueError:
            print(f"⚠ --human 的位置必须是整数：{spec}")
            continue
        out.append((after, parts[1].strip() or "群友", parts[2].strip()))
    return out


# ── 主流程 ──────────────────────────────────────────────────────────────────


def load_one(playbook_id: str, directory: Path) -> Optional[Playbook]:
    """按 id 取一个剧本，顺带把校验结果打出来。"""
    books = load_playbook_dir(directory)
    if not books:
        print(f"✖ {directory} 下没有可用剧本")
        return None
    book = books.get(playbook_id)
    if book is None:
        print(f"✖ 找不到剧本 {playbook_id}；现有：{'、'.join(sorted(books))}")
        return None

    problems = validate_playbook(book)
    hard = [p for p in problems if not p.startswith("warn:")]
    for problem in problems:
        print(("  ⚠ " if problem.startswith("warn:") else "  ✖ ")
              + problem.removeprefix("warn: "))
    if hard:
        print("✖ 剧本有硬错，先修剧本再排练")
        return None
    return book


async def run(args: argparse.Namespace) -> int:
    directory = Path(args.dir) if args.dir else PLAYBOOK_DIR

    if args.list:
        books = load_playbook_dir(directory)
        if not books:
            print(f"（{directory} 下没有剧本）")
            return 1
        print(f"═══ 剧本库 {directory} ═══")
        for pid, book in sorted(books.items()):
            problems = validate_playbook(book)
            hard = sum(1 for p in problems if not p.startswith("warn:"))
            flag = f"  ✖{hard} 处硬错" if hard else ""
            print(f"  {pid:<24} {book.name}  "
                  f"[{book.system}] {len(book.beats)} 拍 "
                  f"soft={book.soft_ad_level}{flag}")
        return 0

    book = load_one(args.playbook, directory)
    if book is None:
        return 1

    # ── 演员与关联体检 ──────────────────────────────────────────────────
    candidates, registry_rows = ([], [])
    if args.real_accounts:
        candidates, registry_rows = real_actors(
            args.platform, db_path=args.registry_db)
        online = [c for c in candidates if c["health"] == HEALTH_OK]
        if not online:
            print("⚠ 注册表里没有在线号，回落占位演员")
            candidates, registry_rows = [], []
    if not candidates:
        candidates = stub_actors(args.actors)

    if registry_rows:
        report = linkage_readiness(registry_rows, host_tag=args.host_tag)
        print(format_readiness(report))
        print()
        fingerprint_groups: Optional[Dict[str, str]] = derive_fingerprint_groups(
            registry_rows, host_tag=args.host_tag)
    else:
        fingerprint_groups = None

    if args.allow_shared_host:
        # 显式放宽：给每个号编一个独立组，等于回到「未知即独立」的旧口径。
        # 只在排练里可用，且必须留下醒目痕迹——否则看结果的人会以为真发也这样。
        fingerprint_groups = {
            str(c.get("account_id")): f"__preview__:{c.get('account_id')}"
            for c in candidates
        }
        print("⚠ --allow-shared-host 已放宽指纹约束：这是**预览口径**，"
              "真发时同出口的号仍然只能上一个\n")

    # ── 生成器 ──────────────────────────────────────────────────────────
    client = None
    if args.llm or args.persona:
        print("… 正在初始化 AI 客户端")
        try:
            client = await build_ai_client()
        except Exception as exc:  # noqa: BLE001
            print(f"✖ AI 客户端初始化失败，回落占位台词：{exc!r}")
            client = None

    if client is not None and args.persona:
        generate = persona_line_generator(client)
        print("… 台词走人设发声链（带 persona_guard 出站校验）")
    elif client is not None:
        generate = llm_generator(client, temperature=args.temperature)
        print("… 台词走 LLM 通用入口（无人设声音）")
    else:
        generate = stub_generator()

    # ── 开演 ────────────────────────────────────────────────────────────
    result = await rehearse(
        book,
        candidates,
        group_key=args.group,
        platform=args.platform,
        generate=generate,
        seed=args.seed,
        human_script=parse_human(args.human),
        fingerprint_groups=fingerprint_groups,
    )

    print()
    print(format_rehearsal(result))

    if args.json:
        import dataclasses
        import json
        print()
        print(json.dumps(dataclasses.asdict(result),
                         ensure_ascii=False, indent=2, default=str))

    # 演出来是空的 / 生成侧不稳 → 退出码非零，别让脚本在 CI 里假装成功
    if result.terminate_reason == "generation_failed" or not result.lines:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="group_show_rehearse",
        description="群戏离线排练（不发任何真消息）",
    )
    p.add_argument("--playbook", default="growth_matrixx", help="剧本 id")
    p.add_argument("--dir", default="", help=f"剧本目录（默认 {PLAYBOOK_DIR}）")
    p.add_argument("--list", action="store_true", help="列出剧本库后退出")
    p.add_argument("--group", default="rehearsal", help="群标识（只用于展示）")
    p.add_argument("--platform", default="telegram")
    p.add_argument("--actors", type=int, default=DEFAULT_STUB_ACTORS,
                   help="占位演员数")
    p.add_argument("--real-accounts", action="store_true",
                   help="从账号注册表取真实在线号（并跑关联体检）")
    p.add_argument("--registry-db", default="", help="账号注册表路径")
    p.add_argument("--host-tag", default="local",
                   help="本机标识（多机部署时区分宿主兜底组）")
    p.add_argument("--allow-shared-host", action="store_true",
                   help="放宽指纹约束以预览完整剧本（仅排练，真发无此口径）")
    p.add_argument("--llm", action="store_true", help="用真 LLM 生成台词")
    p.add_argument("--persona", action="store_true",
                   help="台词走人设发声链（隐含 --llm）")
    p.add_argument("--temperature", type=float, default=0.9)
    p.add_argument("--seed", type=int, default=0, help="节奏随机种子")
    p.add_argument("--human", action="append", default=[],
                   metavar="位置:谁:说什么", help="模拟真人插话，可重复")
    p.add_argument("--json", action="store_true", help="附带输出 JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n（已中断）")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
