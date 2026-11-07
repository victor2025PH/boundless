"""WhatsApp 号码闭环的多机真发验收。

不启动 Telegram、关怀调度或旧 RPA 轮询。收件箱写在 --data-dir。
adb 只通过 ssh 在远端执行，禁止 kill-server、start-server、connect。

在 117 上验收（ssh 到 hub176 跑 adb，PP-OCR 在 192.168.0.176:8766）。
先在当前窗口设置决定接口的令牌，不要把令牌写进命令行::

    set WA_DECIDE_TOKEN=从本机配置读取的同一枚令牌

干跑（打开预填、核对中文、不点发送）::

    python scripts\\wa_phone_loop_acceptance.py dry-run ^
      --data-dir D:\\wa-accept\\data --evidence-dir D:\\wa-accept\\dry ^
      --adb-ssh-host hub176 --serial SERIAL_A ^
      --ocr-url http://192.168.0.176:8766 ^
      --account seat-a --phone +639000000001 --text 你好，这是验收

两台手机对聊。不传 --decide-url 时用 --config 在进程内走同一套人设生成，
不拉起后台服务。18799 已经在跑时可以改用 --decide-url::

    python scripts\\wa_phone_loop_acceptance.py pair-chat ^
      --data-dir D:\\wa-accept\\data --evidence-dir D:\\wa-accept\\pair ^
      --adb-ssh-host hub176 --ocr-url http://192.168.0.176:8766 ^
      --config D:\\chengjie-instances\\zhiliao\\config\\config.yaml ^
      --a-serial SERIAL_A --a-phone +639000000001 --a-account seat-a ^
      --b-serial SERIAL_B --b-phone +639000000002 --b-account seat-b ^
      --turns 4 --seed 你好，我在做链路验收

    python scripts\\wa_phone_loop_acceptance.py pair-chat ^
      --data-dir D:\\wa-accept\\data --evidence-dir D:\\wa-accept\\pair ^
      --adb-ssh-host hub176 --ocr-url http://192.168.0.176:8766 ^
      --decide-url http://127.0.0.1:18799/api/replybus/decide ^
      --a-serial SERIAL_A --a-phone +639000000001 --a-account seat-a ^
      --b-serial SERIAL_B --b-phone +639000000002 --b-account seat-b ^
      --turns 4 --seed 你好，我在做链路验收

冷启动上限（默认连发 cap+1 条，最后一条应被拦住）::

    python scripts\\wa_phone_loop_acceptance.py first-touch-cap ^
      --data-dir D:\\wa-accept\\data --evidence-dir D:\\wa-accept\\cap ^
      --adb-ssh-host hub176 --serial SERIAL_A ^
      --ocr-url http://192.168.0.176:8766 ^
      --account seat-a --phone +639000000099 --cap 2 ^
      --text 验收冷启动一 --text 验收冷启动二

拒收（喂入「不要再发」后，下一句不得点发送）::

    python scripts\\wa_phone_loop_acceptance.py stop-test ^
      --data-dir D:\\wa-accept\\data --evidence-dir D:\\wa-accept\\stop ^
      --adb-ssh-host hub176 --serial SERIAL_A ^
      --ocr-url http://192.168.0.176:8766 ^
      --account seat-a --phone +639000000001 --text 不要再发

转化待确认（不加 --confirm 不会记成交）::

    python scripts\\wa_phone_loop_acceptance.py convert-pending ^
      --data-dir D:\\wa-accept\\data --evidence-dir D:\\wa-accept\\deal ^
      --adb-ssh-host hub176 --serial SERIAL_A ^
      --ocr-url http://192.168.0.176:8766 ^
      --account seat-a --phone +639000000001 --text 我下单了

频繁提示检查（只截图，不发送）::

    python scripts\\wa_phone_loop_acceptance.py rate-limit-check ^
      --data-dir D:\\wa-accept\\data --evidence-dir D:\\wa-accept\\rate ^
      --adb-ssh-host hub176 --serial SERIAL_A ^
      --ocr-url http://192.168.0.176:8766
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.inbox.store import InboxStore
from src.integrations.whatsapp_rpa.phone_acceptance import (
    TOKEN_ENV_NAMES,
    EvidenceLog,
    RecordingDevice,
    SshAdbPhoneDevice,
    make_http_decide,
    make_ppocr_reader,
    run_convert_pending,
    run_dry,
    run_first_touch_cap,
    run_pair_chat,
    run_rate_limit_check,
    run_stop_test,
)
from src.integrations.whatsapp_rpa.phone_ledger import PhoneLedger
from src.integrations.whatsapp_rpa.phone_loop import WaPhoneLoop


def read_token() -> str:
    for name in TOKEN_ENV_NAMES:
        value = os.environ.get(name) or ""
        if value:
            return value
    return ""


def build_decide(args, inbox):
    if getattr(args, "decide_url", ""):
        return make_http_decide(args.decide_url, token=read_token())
    if getattr(args, "config", ""):
        return build_inprocess_decide(Path(args.config), Path(args.data_dir), inbox)
    raise SystemExit("需要 --config（进程内人设）或 --decide-url（已运行的 decide）")


def build_inprocess_decide(config_path: Path, data_dir: Path, inbox):
    """进程内调用 decide_whatsapp。不启动 Telegram、关怀或旧 RPA。"""
    from types import SimpleNamespace

    from src.ai.ai_client import AIClient
    from src.integrations.whatsapp_rpa.phone_loop import decide_whatsapp
    from src.skills.skill_manager import SkillManager
    from src.utils.config_manager import ConfigManager

    manager = ConfigManager(str(config_path))
    cfg = manager.config if isinstance(getattr(manager, "config", None), dict) else {}
    memory = cfg.setdefault("memory", {})
    if isinstance(memory, dict):
        memory["db_path"] = str(Path(data_dir) / "acceptance_memory.db")
    ai_client = AIClient(manager)
    skill = SkillManager(manager, ai_client)
    app = SimpleNamespace(state=SimpleNamespace(
        skill_manager=skill,
        ai_client=ai_client,
        inbox_store=inbox,
        config_manager=manager,
    ))

    async def decide(*, account_id: str, phone: str, last_inbound: str, persona_id: str = ""):
        return await decide_whatsapp(
            app, account_id=account_id, phone=phone,
            last_inbound=last_inbound, persona_id=persona_id,
        )

    return decide


def build_device(args, evidence: EvidenceLog, *, serial: str, label: str, reader):
    if not args.adb_ssh_host or not serial:
        raise SystemExit("真机验收需要 --adb-ssh-host 和 --serial")
    inner = SshAdbPhoneDevice(args.adb_ssh_host, serial, reader)
    return RecordingDevice(inner, evidence, label)


def make_loop(args, inbox, device, ledger_path: Path, account: str, decide):
    return WaPhoneLoop(
        account_id=account,
        inbox=inbox,
        device=device,
        decide_fn=decide,
        cfg={
            "enabled": True,
            "hourly_cap": int(getattr(args, "cap", 2) or 2),
            "daily_cap": 80,
            "reply_mode": "auto",
        },
        ledger_path=ledger_path,
        persona_id=str(getattr(args, "persona_id", "") or ""),
    )


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", required=True)
    common.add_argument("--evidence-dir", required=True)
    common.add_argument("--adb-ssh-host", default="hub176")
    common.add_argument("--serial", default="")
    common.add_argument("--ocr-url", default="http://192.168.0.176:8766")
    common.add_argument("--config", default="")
    common.add_argument("--decide-url", default="")
    common.add_argument("--account", default="seat-a")
    common.add_argument("--phone", default="")
    common.add_argument("--persona-id", default="")
    top = argparse.ArgumentParser(description="WhatsApp 号码闭环真机验收")
    sub = top.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run", parents=[common])
    dry.add_argument("--text", default="你好，这是验收")

    pair = sub.add_parser("pair-chat", parents=[common])
    pair.add_argument("--a-serial", required=True)
    pair.add_argument("--a-phone", required=True)
    pair.add_argument("--a-account", default="seat-a")
    pair.add_argument("--b-serial", required=True)
    pair.add_argument("--b-phone", required=True)
    pair.add_argument("--b-account", default="seat-b")
    pair.add_argument("--turns", type=int, default=2)
    pair.add_argument("--seed", default="你好，我在做链路验收")

    cap = sub.add_parser("first-touch-cap", parents=[common])
    cap.add_argument("--cap", type=int, default=2)
    cap.add_argument("--text", action="append", default=[])

    stop = sub.add_parser("stop-test", parents=[common])
    stop.add_argument("--text", default="不要再发")

    deal = sub.add_parser("convert-pending", parents=[common])
    deal.add_argument("--text", default="我下单了")
    deal.add_argument("--confirm", action="store_true")

    sub.add_parser("rate-limit-check", parents=[common])
    return top


async def async_main(args, *, devices=None, decide_fn=None, ocr_reader=None) -> dict:
    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    token = read_token() if getattr(args, "decide_url", "") else ""
    evidence = EvidenceLog(Path(args.evidence_dir), secrets=[token])
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    inbox = InboxStore(data / "inbox.db")
    reader = ocr_reader or make_ppocr_reader(args.ocr_url)
    decide = decide_fn
    if decide is None and args.command in {"pair-chat"}:
        decide = build_decide(args, inbox)

    if args.command == "dry-run":
        device = (devices or {}).get("a") or build_device(args, evidence, serial=args.serial, label="a", reader=reader)
        loop = make_loop(args, inbox, device, data / "ledger.db", args.account, decide or _unused_decide)
        report = await run_dry(loop, device, args.phone, args.text, evidence)
    elif args.command == "first-touch-cap":
        device = (devices or {}).get("a") or build_device(args, evidence, serial=args.serial, label="a", reader=reader)
        loop = make_loop(args, inbox, device, data / "ledger.db", args.account, decide or _unused_decide)
        report = await run_first_touch_cap(loop, args.phone, args.text, args.cap, evidence)
    elif args.command == "stop-test":
        device = (devices or {}).get("a") or build_device(args, evidence, serial=args.serial, label="a", reader=reader)
        loop = make_loop(args, inbox, device, data / "ledger.db", args.account, decide or _unused_decide)
        report = await run_stop_test(loop, args.phone, args.text, evidence)
    elif args.command == "convert-pending":
        device = (devices or {}).get("a") or build_device(args, evidence, serial=args.serial, label="a", reader=reader)
        loop = make_loop(args, inbox, device, data / "ledger.db", args.account, decide or _unused_decide)
        report = await run_convert_pending(loop, args.phone, args.text, evidence, confirm=args.confirm)
    elif args.command == "rate-limit-check":
        device = (devices or {}).get("a") or build_device(args, evidence, serial=args.serial, label="a", reader=reader)
        report = await run_rate_limit_check(device, evidence)
    elif args.command == "pair-chat":
        if decide is None:
            decide = build_decide(args, inbox)
        device_a = (devices or {}).get("a") or build_device(
            args, evidence, serial=args.a_serial, label="a", reader=reader)
        device_b = (devices or {}).get("b") or build_device(
            args, evidence, serial=args.b_serial, label="b", reader=reader)
        loop_a = make_loop(args, inbox, device_a, data / "ledger-a.db", args.a_account, decide)
        loop_b = make_loop(args, inbox, device_b, data / "ledger-b.db", args.b_account, decide)
        report = await run_pair_chat(
            loop_a, loop_b, args.a_phone, args.b_phone, args.turns, args.seed, evidence,
        )
    else:
        raise SystemExit(f"unknown command {args.command}")
    print(json.dumps({k: v for k, v in report.items() if k != "steps"}, ensure_ascii=False, indent=2))
    return report


async def _unused_decide(**kwargs):
    return {"ok": False, "reply": ""}


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    report = asyncio.run(async_main(args))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
