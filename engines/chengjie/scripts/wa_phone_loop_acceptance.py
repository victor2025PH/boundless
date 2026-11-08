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
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.inbox.store import InboxStore
from src.integrations.whatsapp_rpa.phone_acceptance import (
    TOKEN_ENV_NAMES,
    DeviceBusy,
    EvidenceLog,
    RecordingDevice,
    SshAdbPhoneDevice,
    lock_serials,
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


async def build_decide(args, inbox):
    if getattr(args, "decide_url", ""):
        return make_http_decide(args.decide_url, token=read_token())
    if getattr(args, "config", ""):
        return await build_inprocess_decide(Path(args.config), Path(args.data_dir), inbox)
    raise SystemExit("需要 --config（进程内人设）或 --decide-url（已运行的 decide）")


def _ai_client_ready(ai_client) -> bool:
    return bool(getattr(ai_client, "_oa_client", None) or getattr(ai_client, "client", None))


_OVERLAY_REPLACE = {"ai.models", "ai.task_routes"}


def _read_yaml_mapping(path: Path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise SystemExit(f"配置不是一份映射，验收停止：{path}")
    return loaded


def _merge_overlay(base: dict, overlay: dict, replace: set, prefix: str = "") -> None:
    for key, value in overlay.items():
        path = f"{prefix}{key}"
        if path not in replace and isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_overlay(base[key], value, replace, path + ".")
        else:
            base[key] = value


class AcceptanceConfig:
    """验收用的只读配置。不调用 ConfigManager.load，因此不校验 Telegram。"""

    def __init__(self, config_path: Path, data: dict) -> None:
        self.config_path = Path(config_path)
        self.config = data

    def get_ai_config(self) -> dict:
        section = self.config.get("ai") or {}
        return section if isinstance(section, dict) else {}

    def get_skills_config(self) -> dict:
        section = self.config.get("skills") or {}
        return section if isinstance(section, dict) else {}

    def get_intent_config(self) -> dict:
        section = self.config.get("intent") or {}
        return section if isinstance(section, dict) else {}

    def get_templates_config(self) -> dict:
        section = self.config.get("templates") or {}
        return section if isinstance(section, dict) else {}

    def get_personas_config(self) -> dict:
        path = self.config_path.parent / "personas.yaml"
        if not path.exists():
            inline = self.config.get("personas") or {}
            return inline if isinstance(inline, dict) else {}
        try:
            return _read_yaml_mapping(path)
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit(f"人设文件读不出来，验收停止：{path} ({exc})") from exc

    def get_personas_file_path(self) -> Path:
        return self.config_path.parent / "personas.yaml"


def load_acceptance_config(config_path: Path, data_dir: Path) -> AcceptanceConfig:
    """只读主配置和同目录 config.local.yaml。不写盘，不跑主进程的校验。"""
    path = Path(config_path)
    if not path.is_file():
        raise SystemExit(f"配置文件不存在，验收停止：{path}")
    try:
        data = _read_yaml_mapping(path)
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f"配置读不出来，验收停止：{path} ({exc})") from exc
    overlay_path = path.parent / "config.local.yaml"
    if overlay_path.is_file():
        try:
            overlay = _read_yaml_mapping(overlay_path)
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit(f"凭证 overlay 读不出来，验收停止：{overlay_path} ({exc})") from exc
        _merge_overlay(data, overlay, _OVERLAY_REPLACE)
    ai = data.get("ai")
    if not isinstance(ai, dict):
        raise SystemExit("配置里没有 ai 段，验收停止。验收只读取 AI（DeepSeek）和人设，不校验 Telegram")
    key = str(ai.get("api_key") or "").strip()
    if not key or key == "YOUR_AI_API_KEY" or key.upper().startswith("YOUR_"):
        raise SystemExit("AI 客户端初始化失败，验收停止。请检查配置里的 DeepSeek（ai.provider / ai.base_url / ai.api_key）")
    try:
        probe = float(ai.get("boot_probe_timeout_sec", 6))
    except (TypeError, ValueError):
        probe = 6.0
    if probe <= 0 or probe > 8:
        ai["boot_probe_timeout_sec"] = 6.0
    memory = data.setdefault("memory", {})
    if isinstance(memory, dict):
        memory["db_path"] = str(Path(data_dir) / "acceptance_memory.db")
    return AcceptanceConfig(path, data)


async def build_inprocess_decide(config_path: Path, data_dir: Path, inbox):
    """进程内调用 decide_whatsapp。只装 AI 和人设，失败就停。

    不调用 ConfigManager.load，因此不会校验 Telegram phone_number，
    也不跑主进程里那些会写盘的迁移。不启动 Telegram、关怀或旧 RPA。
    """
    from types import SimpleNamespace

    from src.ai.ai_client import AIClient
    from src.integrations.whatsapp_rpa.phone_loop import decide_whatsapp
    from src.skills.skill_manager import SkillManager

    manager = load_acceptance_config(config_path, data_dir)
    ai_client = AIClient(manager)
    if not await ai_client.initialize() or not _ai_client_ready(ai_client):
        raise SystemExit("AI 客户端初始化失败，验收停止。请检查配置里的 DeepSeek（ai.provider / ai.base_url / ai.api_key）")
    skill = SkillManager(manager, ai_client)
    if not await skill.initialize():
        raise SystemExit("技能管理器初始化失败，验收停止")
    app = SimpleNamespace(state=SimpleNamespace(
        skill_manager=skill,
        ai_client=ai_client,
        inbox_store=inbox,
        config_manager=manager,
    ))

    async def decide(*, account_id: str, phone: str, last_inbound: str, persona_id: str = "",
                     agent_instruction: str = ""):
        return await decide_whatsapp(
            app, account_id=account_id, phone=phone,
            last_inbound=last_inbound, persona_id=persona_id,
            agent_instruction=agent_instruction,
        )

    return decide


def persona_display_name(manager: Any, persona_id: str) -> str:
    """从人设配置里取名字。没有名字就用人设 id。"""
    pid = str(persona_id or "").strip()
    if manager is None or not pid:
        return pid
    try:
        data = manager.get_personas_config() or {}
    except Exception:
        return pid
    profiles = data.get("profiles") or []
    if isinstance(profiles, dict):
        profiles = list(profiles.values())
    for row in profiles:
        if isinstance(row, dict) and str(row.get("id") or "") == pid:
            return str(row.get("name") or pid)
    return pid


def build_device(args, evidence: EvidenceLog, *, serial: str, label: str, reader):
    if not args.adb_ssh_host or not serial:
        raise SystemExit("真机验收需要 --adb-ssh-host 和 --serial")
    inner = SshAdbPhoneDevice(args.adb_ssh_host, serial, reader)
    inner.open_timeout = float(args.open_timeout)
    return RecordingDevice(inner, evidence, label)


def make_loop(args, inbox, device, ledger_path: Path, account: str, decide, persona_id: str = ""):
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
        persona_id=str(persona_id or getattr(args, "persona_id", "") or ""),
        run_id=str(getattr(args, "run_id", "") or ""),
    )


def bind_persona_decide(decide, self_id: str, self_name: str, peer_name: str):
    from src.integrations.whatsapp_rpa.phone_loop import persona_separation_note

    note = persona_separation_note(self_name, peer_name)

    async def wrapped(*, account_id: str, phone: str, last_inbound: str, persona_id: str = ""):
        return await decide(
            account_id=account_id, phone=phone, last_inbound=last_inbound,
            persona_id=self_id or persona_id, agent_instruction=note,
            self_name=self_name, peer_names=[peer_name] if peer_name else [],
        )

    return wrapped


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
    common.add_argument("--open-timeout", type=float, default=25.0)
    top = argparse.ArgumentParser(description="WhatsApp 号码闭环真机验收")
    sub = top.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run", parents=[common])
    dry.add_argument("--text", default="你好，这是验收")

    pair = sub.add_parser("pair-chat", parents=[common])
    pair.add_argument("--a-serial", required=True)
    pair.add_argument("--a-phone", required=True)
    pair.add_argument("--a-account", default="seat-a")
    pair.add_argument("--a-persona", default="")
    pair.add_argument("--b-serial", required=True)
    pair.add_argument("--b-phone", required=True)
    pair.add_argument("--b-account", default="seat-b")
    pair.add_argument("--b-persona", default="")
    pair.add_argument("--turns", type=int, default=2)
    pair.add_argument("--seed", default="你好，我在做链路验收")
    pair.add_argument("--max-seconds", type=float, default=540)

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
    if not getattr(args, "run_id", ""):
        args.run_id = uuid.uuid4().hex[:12]
    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    serials = []
    if args.command == "pair-chat":
        serials = [args.a_serial, args.b_serial]
    elif getattr(args, "serial", ""):
        serials = [args.serial]
    try:
        locks = lock_serials(data, serials)
    except DeviceBusy as busy:
        raise SystemExit(str(busy)) from busy
    try:
        report = await _run_command(
            args, data, locks, devices=devices, decide_fn=decide_fn, ocr_reader=ocr_reader,
        )
    finally:
        for lock in locks:
            lock.release()
    return report


async def _run_command(args, data, locks, *, devices=None, decide_fn=None, ocr_reader=None) -> dict:
    """初始化只碰本地文件和本机锁。

    dry-run、first-touch-cap、stop-test、convert-pending、rate-limit-check
    不读 ``--config``，不连 OCR，也不连 ssh；这些都留到命令真正收发时。
    只有 pair-chat 会装 AI。``--config`` 走只读加载，探针最长 8 秒。
    ``--decide-url`` 在真正要回复时才发请求。
    """
    del locks
    token = read_token() if getattr(args, "decide_url", "") else ""
    evidence = EvidenceLog(Path(args.evidence_dir), secrets=[token])
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    inbox = InboxStore(data / "inbox.db")
    reader = ocr_reader or make_ppocr_reader(args.ocr_url)
    decide = decide_fn
    if decide is None and args.command in {"pair-chat"}:
        decide = await build_decide(args, inbox)
    for device in (devices or {}).values():
        device.open_timeout = float(args.open_timeout)

    try:
        report = await _dispatch(
            args, data, evidence, inbox, reader, decide, devices=devices,
        )
    except Exception as exc:
        evidence.checkpoint({
            "ok": False, "command": args.command, "error": type(exc).__name__,
        })
        raise
    return report


async def _dispatch(args, data, evidence, inbox, reader, decide, *, devices=None) -> dict:
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
            decide = await build_decide(args, inbox)
        device_a = (devices or {}).get("a") or build_device(
            args, evidence, serial=args.a_serial, label="a", reader=reader)
        device_b = (devices or {}).get("b") or build_device(
            args, evidence, serial=args.b_serial, label="b", reader=reader)
        a_persona = str(getattr(args, "a_persona", "") or "")
        b_persona = str(getattr(args, "b_persona", "") or "")
        if not a_persona or not b_persona or a_persona == b_persona:
            raise SystemExit("pair-chat 需要两个不同的人设：--a-persona 和 --b-persona")
        a_name = persona_display_name(None, a_persona)
        b_name = persona_display_name(None, b_persona)
        if getattr(args, "config", ""):
            try:
                named = load_acceptance_config(args.config, data)
                a_name = persona_display_name(named, a_persona)
                b_name = persona_display_name(named, b_persona)
            except Exception:
                pass
        loop_a = make_loop(
            args, inbox, device_a, data / "ledger-a.db", args.a_account,
            bind_persona_decide(decide, a_persona, a_name, b_name), persona_id=a_persona,
        )
        loop_b = make_loop(
            args, inbox, device_b, data / "ledger-b.db", args.b_account,
            bind_persona_decide(decide, b_persona, b_name, a_name), persona_id=b_persona,
        )
        report = await run_pair_chat(
            loop_a, loop_b, args.a_phone, args.b_phone, args.turns, args.seed, evidence,
            max_seconds=float(getattr(args, "max_seconds", 540) or 540),
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
