"""托管租户实例生命周期（provision_instance 确定性半程的后半程）。

背景（托管 SaaS 档「一客一实例」）
==================================
`scripts/provision_instance.py`（Sprint5）负责规划与写盘：分配 id/端口/数据根、渲染
overlay、登记 stack.json——刻意止步于「不拉起、不签发、不建 junction」。本模块补齐
机器侧剩下的全部生命周期，使「开通/暂停/恢复/退租」各是一条命令（CLI 见
`scripts/tenant_ops.py`）：

- **materialize**：数据根骨架 + config 播种 + overlay 落盘（幂等，绝不覆盖已有数据）；
- **junction**：数据根 `domains` → 引擎 `domains`（域包/人设/KB 种子随代码走）；
- **就绪等待**：轮询 `/login` 200（登录页免鉴权，与 README §10 试点同判据）；
- **suspend/resume**：欠费/退租停机 = 停进程 + 落 `\.ops\suspended\<iid>.flag`
  （语义对齐 cutover 的 retired.flag：将来 watchdog 扩展到租户实例时，见 flag 即跳过，
  防 5min 节拍把欠费实例拉活）；恢复 = 删 flag + 再拉起；
- **export**：退租数据导出（客户资产 = 会话/联系人/KB/配置；登录态与授权默认不随包，
  见 `export_manifest` 的取舍注释）。

分层约定：判定/清单/路径全部纯函数（可单测）；进程/网络副作用集中在少数执行函数，
且全部带防呆（只动「持有该租户端口 + 命令行含 main.py + cwd 落该租户数据根」的进程，
绝不触碰 zhiliao/tongyi 生产实例——`CORE_INSTANCE_IDS` 硬拒）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 生产双实例（含已退役占位）——生命周期命令对它们硬拒，防误操作生产。
CORE_INSTANCE_IDS = {"zhiliao", "tongyi"}
CORE_SERVICE_IDS = {"chengjie", "chengjie_zhiliao", "chengjie_tongyi"}

#: 运维状态区（与 restart_cooldown / retired 同层，见 deploy/instances/README §12）
DEFAULT_OPS_BASE = r"D:\chengjie-instances\.ops"


# ────────────────────────── 纯函数：识别 / 路径 ──────────────────────────

def is_tenant_service(svc: Dict[str, Any]) -> bool:
    """stack.json service 条目是否为「托管租户实例」（排除生产双实例与非 chengjie 服务）。"""
    sid = str(svc.get("id") or "")
    return sid.startswith("chengjie_") and sid not in CORE_SERVICE_IDS


def tenant_services(stack: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [s for s in (stack or {}).get("services", []) or [] if is_tenant_service(s)]


def instance_id_of(svc: Dict[str, Any]) -> str:
    """chengjie_<iid> → <iid>。"""
    return str(svc.get("id") or "").removeprefix("chengjie_")


def guard_not_core(instance_id: str) -> None:
    """生命周期命令的硬闸：绝不把生产实例当租户操作。"""
    iid = (instance_id or "").strip().lower()
    if iid in CORE_INSTANCE_IDS or f"chengjie_{iid}" in CORE_SERVICE_IDS:
        raise ValueError(
            f"{instance_id!r} 是生产实例，不属于租户生命周期管辖；"
            "生产操作走 deploy/instances/restart_instance.ps1 等唯一入口")


def suspended_flag_path(instance_id: str, ops_base: str = DEFAULT_OPS_BASE) -> Path:
    return Path(ops_base) / "suspended" / f"{instance_id}.flag"


def exports_dir(ops_base: str = DEFAULT_OPS_BASE) -> Path:
    return Path(ops_base) / "exports"


def tenant_card_path(data_dir: str) -> Path:
    """交付卡落数据根同级（D:\\chengjie-instances\\<iid>\\tenant_card.json）——
    开通守护（下一阶段对接官网订单）直接读这张卡回填订单。"""
    return Path(data_dir).parent / "tenant_card.json"


def data_dir_of_service(svc: Dict[str, Any], data_base: str = r"D:\chengjie-instances") -> str:
    """从 stack 条目推数据根：优先解析 up.args 的 -DataDir，回落目录约定。"""
    args = str(((svc.get("up") or {}).get("args")) or "")
    marker = "-DataDir \""
    if marker in args:
        rest = args.split(marker, 1)[1]
        if '"' in rest:
            return rest.split('"', 1)[0]
    return rf"{data_base}\{instance_id_of(svc)}\data"


def web_port_of_service(svc: Dict[str, Any]) -> Optional[int]:
    ports = svc.get("ports") or []
    try:
        return int(ports[0]) if ports else None
    except (TypeError, ValueError):
        return None


# ────────────────────────── 纯函数：materialize 计划 ──────────────────────────

SKELETON_SUBDIRS = ("config", "sessions", "logs", "events\\spool", "ledger_outbox")


def materialize(
    data_dir: str,
    overlay_text: str,
    example_cfg: Path,
) -> List[str]:
    """建数据根骨架 + 播种 config.yaml + 写 overlay。幂等：已存在的文件一律跳过不覆盖。

    与 provision_instance.py --apply 的写盘段等价（那边是 CLI 内联实现）；收敛方向是
    provision_instance 将来改调本函数，本期不动它（它有自己的输出契约与在用调用方）。
    返回执行动作清单（人读 + 测试断言用）。
    """
    actions: List[str] = []
    root = Path(data_dir)
    for sub in SKELETON_SUBDIRS:
        p = root / sub
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            actions.append(f"mkdir {p}")
    cfg = root / "config" / "config.yaml"
    if cfg.exists():
        actions.append("config.yaml 已存在，跳过（不覆盖）")
    elif example_cfg.exists():
        shutil.copy2(example_cfg, cfg)
        actions.append(f"config.yaml ← {example_cfg.name}")
    else:
        actions.append(f"警告: 缺 {example_cfg}，config.yaml 未创建")
    overlay = root / "config" / "config.local.yaml"
    if overlay.exists():
        actions.append("config.local.yaml 已存在，跳过（不覆盖）")
    else:
        overlay.write_text(overlay_text, encoding="utf-8")
        actions.append("config.local.yaml 已写（含随机机密）")
    return actions


def junction_command(data_dir: str, engine_domains: str) -> List[str]:
    """domains junction 创建命令（mklink /J 无需管理员权限）。"""
    return ["cmd", "/c", "mklink", "/J",
            str(Path(data_dir) / "domains"), str(engine_domains)]


def ensure_junction(data_dir: str, engine_domains: str) -> str:
    """幂等建 domains junction；返回动作描述。"""
    link = Path(data_dir) / "domains"
    if link.exists():
        return "domains junction 已存在，跳过"
    target = Path(engine_domains)
    if not target.exists():
        raise FileNotFoundError(f"引擎 domains 目录不存在: {target}")
    r = subprocess.run(junction_command(data_dir, engine_domains),
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"mklink /J 失败: {(r.stderr or r.stdout).strip()}")
    return f"domains junction → {target}"


# ────────────────────────── 纯函数：进程目标判定 ──────────────────────────

@dataclass
class ProcDesc:
    """进程画像（psutil 采集后的纯数据，判定函数只吃它——可单测）。"""
    pid: int
    cmdline: str
    cwd: str = ""


def select_engine_pids(candidates: Sequence[ProcDesc], data_dir: str) -> List[int]:
    """从「持有租户端口的进程」里挑出可以停的引擎进程。

    防呆判据（对齐 stop_instance.ps1 并加严）：
    - 命令行含 main.py（同 stop_instance 标准）——排除偶然占端口的无关进程；
    - cwd 可读时必须落在该租户数据根下（start_zhiliao.ps1 设 CurrentDirectory=数据根）
      ——排除「另一个实例误绑端口」的极端情况；cwd 不可读（权限）时回落命令行判据。
    """
    want = str(Path(data_dir)).lower().rstrip("\\")
    out: List[int] = []
    for p in candidates:
        cl = (p.cmdline or "").lower()
        if "main.py" not in cl:
            continue
        cwd = (p.cwd or "").lower().rstrip("\\")
        if cwd and not cwd.startswith(want):
            continue
        out.append(p.pid)
    return out


# ────────────────────────── 纯函数：导出清单 ──────────────────────────

#: 退租导出的默认排除（相对 config/）：
#: - license.key：授权是厂商签发凭证，不属客户数据资产（托管档换机即失效，导出徒增泄露面）；
#: - license_quota.db：计量台账属厂商记账（且默认在引擎根，见 README §0 例外②，此处防御性排除）；
#: - *.bak / 临时件。
EXPORT_CONFIG_EXCLUDES = {"license.key", "license_quota.db"}


def export_manifest(
    data_dir: str,
    include_sessions: bool = False,
    include_logs: bool = False,
) -> List[Tuple[str, str]]:
    """构建导出清单 [(绝对路径, zip 内路径)]。

    客户资产口径：config/ 全套（DB=会话/联系人/KB/人设/翻译记忆 + yaml 配置 + presets 等
    子目录），sessions/（平台登录态）默认**不**随包——登录态外流等于把客户账号的
    可登录凭证交给拿到包的任何人，退租场景客户在自己环境重新扫码更安全；显式
    `include_sessions=True` 才带。logs 同理默认不带（体积大且非资产）。
    """
    root = Path(data_dir)
    out: List[Tuple[str, str]] = []

    def _walk(base: Path, arc_prefix: str, excludes: Iterable[str] = ()) -> None:
        if not base.exists():
            return
        ex = {e.lower() for e in excludes}
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            if f.name.lower() in ex:
                continue
            if f.suffix.lower() in (".bak", ".tmp"):
                continue
            out.append((str(f), f"{arc_prefix}/{f.relative_to(base).as_posix()}"))

    _walk(root / "config", "config", EXPORT_CONFIG_EXCLUDES)
    if include_sessions:
        _walk(root / "sessions", "sessions")
    if include_logs:
        _walk(root / "logs", "logs")
    return out


def write_export_zip(manifest: Sequence[Tuple[str, str]], out_zip: Path) -> int:
    """按清单落 zip；返回文件数。调用方保证实例已停（在线打包 SQLite 有一致性风险）。"""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arc in manifest:
            zf.write(src, arc)
            n += 1
    return n


# ────────────────────────── 交付卡 ──────────────────────────

def build_tenant_card(
    plan_dict: Dict[str, Any],
    auth_token: str,
    *,
    status: str = "provisioned",
    note: str = "",
    owner_user: str = "",
    owner_password: str = "",
) -> Dict[str, Any]:
    """开通交付卡：给到客户/开通守护的全部信息（下一阶段回填官网订单用）。

    **凭据职责分离**（2026-08-06 决议，见 create_owner_account 的实测依据）：
    - ``username``/``initial_password`` = 交付给客户的 ``owner`` 账号（可自助改密止血）；
    - ``auth_token`` = **我方运维应急通道，绝不进交付串**（改密不会使其失效）。
    未建 owner 账号时（老卡/实例没起）回落 admin+token 的旧口径，保持向后兼容。

    **落地页刻意用 `/workspace/dash` 而非 `/workspace`**：新租户的「首登三步引导」块
    （读 `/api/setup/checklist`，红灯强制显示、绿灯自动隐藏、每项带直达修复入口）
    渲染在今日概览页上。实测新租户返回 red（AI 未配 + 无渠道两项 fail），正是客户
    进门就该看到的东西。``start_here_url`` 给完整自检清单页。

    ``login_url`` 带 ``?next=/workspace/dash``：登录路由已支持安全回跳，交付链接点开即可
    首登直达自检看板（不再绕 `/cases`）。暴露公网后由 ``apply_public_base`` 改写三 URL。
    """
    port = plan_dict.get("web_port")
    user = owner_user or "admin"
    pw = owner_password or auth_token
    local_base = f"http://127.0.0.1:{port}"
    return {
        "instance_id": plan_dict.get("instance_id"),
        "service_id": plan_dict.get("service_id"),
        "product": plan_dict.get("product"),
        "customer": plan_dict.get("customer"),
        "data_dir": plan_dict.get("data_dir"),
        "web_port": port,
        "workspace_url": f"{local_base}/workspace/dash",
        "start_here_url": f"{local_base}/workspace/golive",
        # 字面深链（不 import web 层）：与 login_redirect.DEFAULT_ONBOARD_NEXT 对齐
        "login_url": f"{local_base}/login?next=/workspace/dash",
        "username": user,
        "initial_password": pw,
        "auth_token": auth_token,
        "ops_only": ["auth_token"],
        "status": status,
        "note": note or ("对外暴露需经反代/隧道；交付给客户的是 owner 账号（可自助改密）；"
                         "auth_token 是我方运维通道，绝不外发；"
                         "客户打开 login_url 登录后直达「上线自检」看板"
                         "（配 AI → 接渠道 → 开自动回复）。"),
        "provisioned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def apply_public_base(public_url: str) -> Dict[str, str]:
    """expose 后把交付卡三 URL 从 127.0.0.1 改写为公网基址（纯函数）。"""
    base = (public_url or "").rstrip("/")
    if not base:
        raise ValueError("public_url 不能为空")
    return {
        "public_url": base,
        "workspace_url": f"{base}/workspace/dash",
        "start_here_url": f"{base}/workspace/golive",
        "login_url": f"{base}/login?next=/workspace/dash",
    }


#: 到期临界窗（天）：剩余 ≤3 天进入 expiring（催续费），<0 为 expired
EXPIRY_WARN_DAYS = 3.0


def expiry_status(card: Dict[str, Any], now: float) -> Tuple[str, Optional[float]]:
    """托管到期判定（纯函数）：交付卡 ``expires_at``（本地时间串）→ 状态 + 剩余天数。

    返回 ('none'|'ok'|'expiring'|'expired', days_left)。
    'none'＝无到期账本（未经守护交付的老卡/试点参考件）——**绝不告警**，宁可漏催不误停；
    格式坏同 'none'（解析不了不猜）。到期治理首版＝告警转人工，不自动 suspend
    （续费单与实例的关联机制未建，自动停有误伤收入风险）。
    """
    raw = str((card or {}).get("expires_at") or "").strip()
    if not raw:
        return ("none", None)
    try:
        exp = time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
    except Exception:  # noqa: BLE001 - 手改坏格式按无账本处理
        return ("none", None)
    days = round((exp - now) / 86400, 1)
    if days < 0:
        return ("expired", days)
    if days <= EXPIRY_WARN_DAYS:
        return ("expiring", days)
    return ("ok", days)


def initial_password_unchanged(data_dir: str, token: str,
                               username: str = "admin") -> Optional[bool]:
    """指定账号是否**仍在用交付初始密码**。**只读零写入**。

    刻意不用 ``WebUserStore.verify``——它会 UPDATE last_login，对租户活库产生写入并
    伪造「最后登录时间」。这里以只读连接取 salt/hash 自行 pbkdf2 比对。

    True=仍是初始密码（该催客户改）/ False=已改 / None=判不了（无库/无该账号/无 token）。
    """
    import hashlib
    import sqlite3

    if not token:
        return None
    db = Path(data_dir) / "config" / "web_users.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT pw_salt, pw_hash FROM web_users WHERE username=?",
                (username,)).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - 库忙/结构异常一律判不了，绝不猜
        return None
    if not row:
        return None
    salt, expected = row[0], row[1]
    try:
        actual = hashlib.pbkdf2_hmac("sha256", str(token).encode("utf-8"), salt, 100_000)
    except Exception:  # noqa: BLE001
        return None
    return bytes(actual) == bytes(expected)


#: 交付给客户的账号名（与我方运维用的 `admin` 分离，见 create_owner_account）
OWNER_USERNAME = "owner"


def create_owner_account(
    data_dir: str,
    username: str = OWNER_USERNAME,
    password: str = "",
) -> Tuple[bool, str]:
    """给租户建**客户自己的**管理账号（凭据职责分离）。返回 (是否新建, 密码)。

    为什么不直接把 ``auth_token`` 交给客户（2026-08-06 实测定论）：引擎首启会用
    ``web_admin.auth_token`` 当 ``admin`` 的密码播种 master 账号，于是那一串同时是
    「admin 的密码」和「令牌直登凭据」。实测确认**客户改密后令牌直登通道仍然有效**
    （token 是 config 独立值，不随改密变化）→ 交付串一旦泄露，客户改密**不能止血**。

    故：客户拿 ``owner``（独立随机密码，可自助改密即完全止血）；``auth_token`` 只留
    我方运维应急通道、**不进交付串**。两条凭据互不相干。

    幂等：账号已存在 → 返回 (False, "")，绝不重置客户已改过的密码。
    """
    import secrets as _secrets
    import sys as _sys

    db = Path(data_dir) / "config" / "web_users.db"
    if not db.is_file():
        raise FileNotFoundError(
            f"租户用户库不存在（实例需先启动过一次）: {db}")
    pw = password or _secrets.token_urlsafe(12)
    engine_root = Path(__file__).resolve().parents[2]
    if str(engine_root) not in _sys.path:
        _sys.path.insert(0, str(engine_root))
    # 复用引擎自己的 store：保证角色枚举/哈希算法/字段与登录端完全一致
    from src.utils.web_user_store import ROLE_MASTER, WebUserStore

    store = WebUserStore(db)
    try:
        if store.get_user(username):
            return False, ""
        created = store.create_user(username, pw, ROLE_MASTER, display_name=username)
        if not created:
            raise RuntimeError(f"建客户账号失败: {username}")
        return True, pw
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - 老版本可能无 close
            pass


# ────────────────────────── 执行函数（副作用集中区）──────────────────────────

def port_listeners(port: int) -> List[ProcDesc]:
    """采集持有端口 LISTEN 的进程画像（psutil；cwd 拿不到就留空）。"""
    import psutil  # 延迟导入：纯函数用例不需要它

    descs: List[ProcDesc] = []
    seen = set()
    for c in psutil.net_connections(kind="tcp"):
        if c.status != psutil.CONN_LISTEN or not c.laddr or c.laddr.port != port:
            continue
        if not c.pid or c.pid in seen:
            continue
        seen.add(c.pid)
        try:
            p = psutil.Process(c.pid)
            cl = " ".join(p.cmdline() or [])
            try:
                cwd = p.cwd()
            except Exception:  # noqa: BLE001 - 权限差异，回落空
                cwd = ""
            descs.append(ProcDesc(pid=c.pid, cmdline=cl, cwd=cwd))
        except Exception:  # noqa: BLE001 - 进程已退出等
            continue
    return descs


def stop_tenant(port: int, data_dir: str, grace_sec: int = 8) -> Dict[str, Any]:
    """停租户实例：soft terminate（进程树）→ 宽限 → kill。只动通过防呆判定的进程。"""
    import psutil

    holders = port_listeners(port)
    if not holders:
        return {"stopped": [], "note": "端口无监听，实例未在跑（幂等）"}
    pids = select_engine_pids(holders, data_dir)
    if not pids:
        raise RuntimeError(
            f"端口 {port} 持有者 {[h.pid for h in holders]} 未通过引擎判定"
            "（命令行无 main.py 或 cwd 不在该租户数据根），拒绝停止——人工核实")
    victims: List[psutil.Process] = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            victims.extend(p.children(recursive=True))
            victims.append(p)
            # 顺带带上父 cmd 壳（start 脚本经 cmd /c 链拉起）
            parent = p.parent()
            if parent and "cmd" in (parent.name() or "").lower():
                victims.append(parent)
        except Exception:  # noqa: BLE001
            continue
    stopped: List[int] = []
    for v in victims:
        try:
            v.terminate()
            stopped.append(v.pid)
        except Exception:  # noqa: BLE001
            continue
    gone, alive = psutil.wait_procs(victims, timeout=max(1, grace_sec))
    for v in alive:
        try:
            v.kill()
        except Exception:  # noqa: BLE001
            continue
    psutil.wait_procs(alive, timeout=5)
    return {"stopped": sorted(set(stopped)), "note": f"soft→{grace_sec}s→hard 完成"}


def wait_ready(port: int, timeout_sec: int = 120, interval: float = 2.0) -> bool:
    """轮询 /login 到 200（登录页免鉴权；README §10 试点同判据）。"""
    deadline = time.monotonic() + timeout_sec
    url = f"http://127.0.0.1:{port}/login"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=4) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 连接拒绝/超时=还没就绪
            pass
        time.sleep(interval)
    return False


def probe_login(port: int, timeout: int = 4) -> Tuple[Optional[int], str]:
    """单发探针：返回 (HTTP 状态, 页面 title)。探不到返回 (None, '')。"""
    url = f"http://127.0.0.1:{port}/login"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
            title = ""
            if "<title>" in body:
                title = body.split("<title>", 1)[1].split("</title>", 1)[0].strip()
            return resp.status, title
    except Exception:  # noqa: BLE001
        return None, ""


def read_overlay_token(data_dir: str) -> str:
    """从实例 overlay 里读 auth_token（交付卡用；读不到返回空串不抛）。"""
    p = Path(data_dir) / "config" / "config.local.yaml"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("auth_token:"):
                return s.split(":", 1)[1].strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def write_suspend_flag(instance_id: str, reason: str, ops_base: str = DEFAULT_OPS_BASE) -> Path:
    flag = suspended_flag_path(instance_id, ops_base)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(
        json.dumps({
            "instance_id": instance_id,
            "reason": reason or "suspended",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "watchdog 扩展到租户实例时须识别本 flag：存在即不自愈拉起",
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return flag


# ────────────────────────── 看门狗（决策纯函数 + 状态）──────────────────────────

@dataclass
class TenantFact:
    """watch 一轮巡检里单个租户的输入事实（执行层采集，决策层只吃它）。"""
    instance_id: str
    desired_running: bool     # 交付卡 status == running（意图 SSOT）
    suspended: bool           # 暂停旗存在
    http_ok: bool             # /login 探针 200


def watch_state_path(ops_base: str = DEFAULT_OPS_BASE) -> Path:
    return Path(ops_base) / "tenant_watch_state.json"


def load_watch_state(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 首跑/损坏都从零开始（watch 状态可再生）
        return {}


def save_watch_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def plan_watch_actions(
    facts: Sequence[TenantFact],
    state: Dict[str, Any],
    now: float,
    *,
    heal_cooldown_sec: int = 600,
    max_fail_streak: int = 3,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """一轮巡检的决策（纯函数）：(决策清单, 新状态)。

    语义（对齐 watchdog_instances 的保守哲学）：
    - 暂停旗 / 交付卡非 running（如 --no-start 刚开通）→ 跳过，streak 清零——
      「运营让它停」不是故障；
    - 探针 200 → ok，streak 清零；
    - DOWN：连续拉起失败 ≥ max_fail_streak → give_up（点名人工，防重启风暴；
      resume/suspend 或 --reset 后重新计数）；上次拉起在冷却窗内 → cooldown 等待
      （拉起后引擎要 20~30s 就绪，5min 节拍的下一轮才算数）；否则 → heal。
    状态含义：fail_streak = 「连续 heal 尝试而下一轮仍 DOWN」的次数。
    """
    decisions: List[Dict[str, Any]] = []
    new_state: Dict[str, Any] = {}
    for f in facts:
        st = dict(state.get(f.instance_id) or {})
        streak = int(st.get("fail_streak") or 0)
        last_heal = float(st.get("last_heal") or 0.0)

        if f.suspended or not f.desired_running:
            decisions.append({"instance_id": f.instance_id, "action": "skip",
                              "reason": "suspended" if f.suspended else "not_desired_running"})
            continue  # 刻意不写 new_state：跳过态不留 streak（复活后从零判）
        if f.http_ok:
            decisions.append({"instance_id": f.instance_id, "action": "ok", "reason": ""})
            new_state[f.instance_id] = {"fail_streak": 0, "last_heal": last_heal}
            continue
        # DOWN 分支
        if streak >= max_fail_streak:
            decisions.append({"instance_id": f.instance_id, "action": "give_up",
                              "reason": f"连续 {streak} 次拉起未活，转人工（resume/--reset 重置）"})
            new_state[f.instance_id] = st
            continue
        if last_heal and (now - last_heal) < heal_cooldown_sec:
            decisions.append({"instance_id": f.instance_id, "action": "cooldown",
                              "reason": f"上次拉起 {int(now - last_heal)}s 前，冷却 {heal_cooldown_sec}s"})
            new_state[f.instance_id] = st
            continue
        decisions.append({"instance_id": f.instance_id, "action": "heal",
                          "reason": f"DOWN（streak={streak}）"})
        new_state[f.instance_id] = {"fail_streak": streak + 1, "last_heal": now}
    return decisions, new_state


# ────────────────────────── 备份（活库安全快照）──────────────────────────

def backups_dir(instance_id: str, ops_base: str = DEFAULT_OPS_BASE) -> Path:
    return Path(ops_base) / "backups" / instance_id


def snapshot_sqlite(src: Path, dst: Path) -> None:
    """SQLite 在线一致性快照（backup API；WAL 活库安全，快照自带合并）。

    注意显式 close：`with sqlite3.connect(...)` 只管事务不关连接——不关的话快照文件
    在 Windows 上保持锁定，TemporaryDirectory 清理会 PermissionError。
    """
    import sqlite3

    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=15)
    try:
        out = sqlite3.connect(dst)
        try:
            con.backup(out)
        finally:
            out.close()
    finally:
        con.close()


def backup_tenant_zip(data_dir: str, out_zip: Path) -> Dict[str, int]:
    """厂商侧灾备包：config 全套（*.db 走快照，其余原样）+ sessions（恢复接待必需）。

    与 export（退租交客户）口径刻意不同：备份是**我们自己的灾备**——license.key、
    登录态都要在内，否则恢复出来的实例不能开工；-wal/-shm 不进包（快照已合并）。
    """
    import tempfile

    root = Path(data_dir)
    counts = {"db_snapshots": 0, "files": 0}
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tenant_bk_") as td, \
            zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        cfg = root / "config"
        for f in sorted(cfg.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(cfg).as_posix()
            low = f.name.lower()
            if low.endswith((".db-wal", ".db-shm", ".bak", ".tmp")):
                continue
            if low.endswith(".db"):
                snap = Path(td) / rel
                snapshot_sqlite(f, snap)
                zf.write(snap, f"config/{rel}")
                counts["db_snapshots"] += 1
            else:
                zf.write(f, f"config/{rel}")
                counts["files"] += 1
        sess = root / "sessions"
        if sess.exists():
            for f in sorted(sess.rglob("*")):
                if f.is_file():
                    zf.write(f, f"sessions/{f.relative_to(sess).as_posix()}")
                    counts["files"] += 1
    return counts


def prune_old_backups(dir_: Path, keep: int) -> List[Path]:
    """按文件名（含时间戳）降序保留 keep 份，返回**应删除**清单（调用方执行删除）。"""
    if keep <= 0:
        return []
    zips = sorted((p for p in dir_.glob("*.zip") if p.is_file()),
                  key=lambda p: p.name, reverse=True)
    return zips[keep:]


def latest_backup(instance_id: str, ops_base: str = DEFAULT_OPS_BASE) -> Optional[Path]:
    """该租户最新一份备份 zip（按文件名时间戳降序）；无则 None。"""
    d = backups_dir(instance_id, ops_base)
    zips = sorted((p for p in d.glob(f"{instance_id}_*.zip") if p.is_file()),
                  key=lambda p: p.name, reverse=True)
    return zips[0] if zips else None


def validate_backup_zip(zip_path: Path) -> List[str]:
    """校验备份包：必须含 config/ 条目，且无路径穿越（zip-slip）。返回成员清单；非法即抛。"""
    if not zip_path.is_file():
        raise FileNotFoundError(f"备份包不存在: {zip_path}")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    if not any(n.startswith("config/") for n in names):
        raise ValueError(f"备份包不含 config/（不是有效租户备份）: {zip_path.name}")
    for n in names:
        norm = n.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/"):
            raise ValueError(f"备份包含非法路径（拒绝解压）: {n}")
    return names


def restore_backup(zip_path: Path, data_dir: str) -> Dict[str, int]:
    """把备份包解压回数据根（config/ + sessions/）。**调用方须保证实例已停**（在线解压
    覆盖正在写的 SQLite = 腐化）。已校验无 zip-slip；返回各段恢复文件数。

    幂等语义：直接覆盖同名文件（DR 场景就是要用备份里的版本盖掉损坏的现场）。
    """
    validate_backup_zip(zip_path)
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    counts = {"config": 0, "sessions": 0, "other": 0}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            arc = info.filename.replace("\\", "/")
            dst = root / arc
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dst, "wb") as out:
                out.write(src.read())
            seg = arc.split("/", 1)[0]
            counts[seg if seg in counts else "other"] += 1
    return counts


# ────────────────────────── AI 中转配置注入 ──────────────────────────

def ai_preset_path(ops_base: str = DEFAULT_OPS_BASE) -> Path:
    """厂商侧租户 AI 预设（运营填一次，开通自动注入）：`.ops/tenant_ai_preset.yaml`。"""
    return Path(ops_base) / "tenant_ai_preset.yaml"


def load_ai_preset(path: Path) -> Dict[str, Any]:
    """读预设文件的 ai: 段；缺文件/无 ai 段返回 {}（跳过注入，不报错）。"""
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        ai = data.get("ai") or {}
        return {str(k): v for k, v in ai.items()} if isinstance(ai, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - 预设写坏了宁可不注入也不让开通失败
        return {}


def inject_ai_preset(overlay_path: Path, ai_cfg: Dict[str, Any]) -> List[str]:
    """把 ai 键逐个深写进租户 overlay（**保注释**，走 set_yaml_key_preserving——
    别再新增裸 yaml.dump 整写 overlay 的路径，见 AGENTS「overlay 写入必须保注释」）。

    返回成功写入的键名清单；helper 不可用（无 ruamel）时抛错——宁可失败也不剃注释。
    """
    if not ai_cfg:
        return []
    from src.utils.config_manager import set_yaml_key_preserving

    written: List[str] = []
    for k, v in ai_cfg.items():
        if not set_yaml_key_preserving(overlay_path, ["ai", str(k)], v):
            raise RuntimeError(
                f"保注释写入失败（ai.{k}）：ruamel 不可用或 overlay 非法，拒绝降级为整写")
        written.append(str(k))
    return written


def mask_secret(v: Any) -> str:
    """输出/日志用脱敏：只露尾 4 位。"""
    s = str(v or "")
    return ("***" + s[-4:]) if len(s) > 6 else "***"


# ────────────────────────── 公网暴露（子域 + 隧道 + nginx）──────────────────────────

PUBLIC_DOMAIN = "bd2026.cc"

#: 子域保留字：官网/基础设施语义，绝不分配给租户。
RESERVED_SLUGS = {
    "www", "api", "app", "mail", "smtp", "admin", "console", "static",
    "download", "downloads", "releases", "cdn", "status", "ns1", "ns2",
}

_SLUG_RE = None  # 惰性编译


def validate_slug(slug: str) -> str:
    """租户子域校验：DNS 安全（小写字母数字连字符、不以连字符首尾、≤40）+ 保留字黑名单。

    返回规范化 slug；非法即抛 ValueError（expose 的第一道闸）。
    """
    global _SLUG_RE
    if _SLUG_RE is None:
        import re
        _SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
    s = (slug or "").strip().lower()
    if not _SLUG_RE.match(s):
        raise ValueError(f"非法子域 {slug!r}：只允许小写字母/数字/连字符，≤40 且不以连字符首尾")
    if s in RESERVED_SLUGS:
        raise ValueError(f"子域 {s!r} 是保留字（官网/基础设施），换一个")
    return s


def default_slug(instance_id: str) -> str:
    """instance_id → 缺省子域（下划线转连字符；DNS 不允许下划线）。"""
    return validate_slug((instance_id or "").replace("_", "-"))


def _nginx_proxy_body(upstream_port: int) -> str:
    """两种暴露形态共用的反代主体：SSE 关缓冲 + 长读超时、WebSocket upgrade、真实 IP/协议头。"""
    return f"""    client_max_body_size 50m;

    location / {{
        proxy_pass http://127.0.0.1:{upstream_port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        # SSE（工作台实时流）：禁缓冲 + 长读超时，否则事件被 nginx 攒批/掐断
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}"""


def render_nginx_site(slug: str, port: int, domain: str = PUBLIC_DOMAIN) -> str:
    """子域形态：443 单块（80 由既有 catch-all 兜底 301+ACME）。

    证书路径按 certbot 缺省布局（expose 流程先 certonly 再落本配置，顺序不可反）。
    前提：<slug>.<domain> 的 A 记录已指向 VPS（泛解析或逐条）。
    """
    fqdn = f"{slug}.{domain}"
    return f"""# tenant {slug} — generated by tenant_ops expose (chengjie hosted tenants)
# upstream = VPS localhost:{port} <- ssh reverse tunnel (tenant_tunnel.ps1 on .117) <- tenant instance
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {fqdn};

    ssl_certificate /etc/letsencrypt/live/{fqdn}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{fqdn}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

{_nginx_proxy_body(port)}
}}
"""


def render_nginx_site_port(slug: str, public_port: int, upstream_port: int,
                           domain: str = PUBLIC_DOMAIN) -> str:
    """端口形态：主域高位端口直挂 TLS（`https://<domain>:<public_port>`）。

    零 DNS 依赖的兜底形态——复用主域既有证书（SAN 已含主域），公网端口用租户
    alt_port（provision 已分配、全局不撞）。子域 DNS 就绪后 expose 会自动走子域形态。
    """
    return f"""# tenant {slug} (port-mode) — generated by tenant_ops expose (chengjie hosted tenants)
# https://{domain}:{public_port} -> VPS localhost:{upstream_port} <- ssh tunnel <- tenant instance
server {{
    listen {public_port} ssl;
    listen [::]:{public_port} ssl;
    server_name {domain};

    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

{_nginx_proxy_body(upstream_port)}
}}
"""


def tunnel_ports_file(ops_base: str = DEFAULT_OPS_BASE) -> Path:
    return Path(ops_base) / "tenant_tunnel_ports.txt"


def tunnel_pid_file(ops_base: str = DEFAULT_OPS_BASE) -> Path:
    return Path(ops_base) / "tenant_tunnel.pid"


def ensure_tunnel_port(port: int, ops_base: str = DEFAULT_OPS_BASE) -> bool:
    """把端口登进隧道清单（幂等）。返回是否新增（新增才需要踢隧道重连）。"""
    f = tunnel_ports_file(ops_base)
    lines: List[str] = []
    if f.exists():
        lines = f.read_text(encoding="utf-8-sig").splitlines()
    existing = {ln.strip() for ln in lines if ln.strip().isdigit()}
    if str(port) in existing:
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        lines = ["# 租户隧道端口清单（tenant_tunnel.ps1 消费；tenant_ops expose 维护）"]
    lines.append(str(port))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def emit_tenant_alert(kind: str, text: str, *, account_id: str = "",
                      debounce_sec: float = 1800.0) -> bool:
    """把租户运维事件推到集团 TG 中继（复用 ops_alert.notify：非阻塞/防抖/无密钥降级）。

    绝不抛——告警失败不能拖垮 watch/backup 本身（它们的职责是自愈/备份，不是告警）。
    返回是否真推送（防抖抑制/无密钥/异常都返回 False）。
    """
    try:
        from src.ops.ops_alert import notify
        return bool(notify(kind, text, account_id=account_id,
                           source="tenant_ops", debounce_sec=debounce_sec))
    except Exception:  # noqa: BLE001
        logging.getLogger("ai_chat_assistant.tenant_ops").debug(
            "emit_tenant_alert 失败（忽略）", exc_info=True)
        return False


def kick_tunnel(ops_base: str = DEFAULT_OPS_BASE) -> str:
    """踢一下隧道（杀当前 ssh，runner 循环按新清单 10s 内重连）；无 pid/进程即静默。"""
    import psutil

    pf = tunnel_pid_file(ops_base)
    try:
        pid = int(pf.read_text(encoding="utf-8-sig").strip())
        p = psutil.Process(pid)
        if "ssh" in (p.name() or "").lower():
            p.kill()
            return f"已踢隧道（pid {pid}），runner 将按新清单重连"
        return f"pid {pid} 不是 ssh（runner 可能未在跑），跳过"
    except FileNotFoundError:
        return "无隧道 pid 文件（runner 未在跑？注册/启动见 tenant_tunnel.ps1 头注释）"
    except Exception as e:  # noqa: BLE001
        return f"踢隧道失败（{e}）——runner 未在跑时属正常"
