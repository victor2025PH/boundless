"""群脉 CrowdX 真发 CLI —— 开演前体检（本机）+ 经运行中实例 API 真发。

为什么不在本进程里直接 ``orchestrator.send``
------------------------------------------
双实例部署下 Telegram session 活在智聊/通译的常驻进程里。独立 CLI 再建一个编排器
会跟生产进程抢同一份 session（轻则互踢、重则封号信号）。所以：

* ``--check``：本机纯体检，**零发送**，不碰编排器；
* ``--live``：把请求打到已启动实例的 ``POST /api/group-show/live``，由那台进程投递。

用法
----
先体检（单号请用 solo 剧本）::

    python -m scripts.group_show_live --check -p solo_matrixx --group -100123 \\
        --account acc1

确认配置锁已开（``companion.group_show.live.enabled: true``）后再真发::

    python -m scripts.group_show_live --live -p solo_matrixx --group -100123 \\
        --account acc1 --confirm-live --base-url http://127.0.0.1:18799
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.companion.group_show.live import (  # noqa: E402
    is_solo_playbook,
    live_enabled,
    plan_live,
)
from src.companion.group_show.linkage import (  # noqa: E402
    derive_fingerprint_groups,
    format_readiness,
    linkage_readiness,
)
from src.companion.group_show.playbook import (  # noqa: E402
    load_playbook_dir,
    validate_playbook,
)

PLAYBOOK_DIR = Path("config/playbooks")
DEFAULT_REGISTRY = Path("config/account_registry.db")


def _real_actors(
    registry_path: Path, *, account_ids: Sequence[str] = (),
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    try:
        from src.integrations.account_registry import AccountRegistry
        rows = AccountRegistry(registry_path).list("telegram") or []
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读账号注册表失败（{exc}）", file=sys.stderr)
        return [], []
    want = {str(a) for a in account_ids if str(a or "")}
    actors: List[Dict[str, Any]] = []
    online: List[Dict[str, Any]] = []
    for r in rows:
        if str(r.get("status") or "") != "online":
            continue
        aid = str(r.get("account_id") or "")
        if want and aid not in want:
            continue
        online.append(r)
        meta = r.get("meta") or {}
        actors.append({
            "account_id": aid,
            "platform": "telegram",
            "persona_id": str(meta.get("persona_id") or r.get("persona_id") or ""),
            "display_name": str(r.get("display_name") or r.get("label") or aid),
            "health": "online",
        })
    return actors, online


def _load_app_config() -> Dict[str, Any]:
    try:
        from src.utils.config_manager import ConfigManager
        import asyncio

        async def _load():
            cm = ConfigManager()
            await cm.load()
            return dict(getattr(cm, "config", None) or {})

        return asyncio.run(_load())
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读配置失败（{exc}），按 live.enabled=关 处理", file=sys.stderr)
        return {}


def _run_check(args: argparse.Namespace) -> int:
    books = load_playbook_dir(PLAYBOOK_DIR)
    if args.list or not args.playbook:
        for pid, pb in sorted(books.items()):
            mark = "solo" if is_solo_playbook(pb) else f"{len(pb.roles)}r"
            hard = [w for w in validate_playbook(pb) if not w.startswith("warn:")]
            print(f"  {'✖' if hard else '✓'} {pid:<22} [{mark}] {pb.name}")
        return 0

    pb = books.get(args.playbook)
    if pb is None:
        print(f"没有这个剧本: {args.playbook}")
        return 1

    actors, rows = _real_actors(Path(args.registry), account_ids=args.account or ())
    if not actors:
        print("[error] 没有可用的在线号（--account 过滤后为空？）")
        return 1

    print(format_readiness(linkage_readiness(rows, host_tag=args.host_tag)))
    print()
    fps = derive_fingerprint_groups(rows, host_tag=args.host_tag)
    app_cfg = _load_app_config()
    print(f"配置锁 companion.group_show.live.enabled = "
          f"{'开' if live_enabled(app_cfg) else '关（缺省）'}")
    print(f"剧本形态 = {'solo（单号可演）' if is_solo_playbook(pb) else '多角色'}")
    if len(actors) == 1 and not is_solo_playbook(pb):
        print("⚠ 只有 1 个号却用了多角色剧本——真发会被 understaffed 拒演，"
              "请改用 solo_matrixx")

    # 假装武装，把选角/指纹/禁演时段跑完；真实锁状态上面已经单独报了
    diag = {"companion": {"group_show": {"live": {"enabled": True}}}}
    plan = plan_live(
        pb, actors, group_key=str(args.group or "check"),
        confirm_live=True, app_config=diag, fingerprint_groups=fps,
        require_outlet=False)
    print(f"选角体检 = {'✓ 可演' if plan.ok else '✖ ' + (plan.reason or 'failed')}")
    for w in plan.warnings:
        print(("  ⚠ " if w.startswith("warn:") else "  · ") + w)
    if plan.casting is not None:
        filled = "  ".join(f"{m.slot}={m.account_id}" for m in plan.casting.members)
        print(f"演员表：{filled or '（空）'}")
        if plan.casting.unfilled:
            print(f"空槽：{', '.join(plan.casting.unfilled)}")
    return 0 if plan.ok else 2


def _run_live(args: argparse.Namespace) -> int:
    if not args.confirm_live:
        print("[error] --live 必须同时传 --confirm-live（参数侧那把锁）")
        return 1
    if not args.group:
        print("[error] --live 必须指定 --group")
        return 1
    if not args.playbook:
        print("[error] --live 必须指定 -p/--playbook")
        return 1

    import urllib.error
    import urllib.request

    body: Dict[str, Any] = {
        "playbook_id": args.playbook,
        "group_key": args.group,
        "platform": args.platform or "telegram",
        "confirm_live": True,
        "check_only": False,
        "account_ids": list(args.account or ()),
        "max_speakers": int(args.max_speakers or 0),
    }
    url = args.base_url.rstrip("/") + "/api/group-show/live"
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if args.bearer:
        headers["Authorization"] = f"Bearer {args.bearer}"
    elif args.cookie:
        headers["Cookie"] = args.cookie
    req = urllib.request.Request(
        url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"[error] HTTP {exc.code}: {detail}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 请求失败：{exc}\n"
              f"  确认实例已启动，且 --base-url / Cookie 或 --bearer 正确")
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("started"):
        print(f"\n已后台开演 session={payload.get('session_id')}——"
              f"进度看导播台「最近场次」或 "
              f"GET {args.base_url.rstrip('/')}/api/group-show/sessions"
              f"?group_key={args.group}")
        return 0
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="群脉 CrowdX 真发 CLI（预检 / 经实例 API 开演）")
    ap.add_argument("--check", action="store_true", help="只做开演前体检，零发送")
    ap.add_argument("--live", action="store_true",
                    help="经运行中实例的 /api/group-show/live 真发（需 --confirm-live）")
    ap.add_argument("--list", action="store_true", help="列出剧本")
    ap.add_argument("-p", "--playbook", default="", help="剧本 id（单号用 solo_*）")
    ap.add_argument("--group", default="", help="群 chat_key")
    ap.add_argument("--account", action="append", default=[],
                    help="限定账号（可多次）；单号场景传一个")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--host-tag", dest="host_tag", default="")
    ap.add_argument("--platform", default="telegram")
    ap.add_argument("--max-speakers", dest="max_speakers", type=int, default=0)
    ap.add_argument("--confirm-live", dest="confirm_live", action="store_true",
                    help="参数侧那把锁（--live 必传）")
    ap.add_argument("--base-url", dest="base_url",
                    default="http://127.0.0.1:18799",
                    help="已启动实例的根地址（智聊默认 18799，通译 18899）")
    ap.add_argument("--cookie", default="",
                    help="登录态 Cookie（浏览器 F12 复制；实例开了鉴权时需要）")
    ap.add_argument("--bearer", default="",
                    help="web_admin.auth_token（Authorization: Bearer）；"
                         "运维脚本优先用它，不必抄 Cookie")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if args.live:
        return _run_live(args)
    return _run_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
