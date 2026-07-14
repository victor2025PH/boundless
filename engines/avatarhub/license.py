# -*- coding: utf-8 -*-
"""license.py — 离线授权 / 激活（产品端验签）。

商业模式：本地一体机 / 私有部署授权交付。**厂商持私钥**签发 `license.key`，
产品仅内置 / 随附**公钥**离线验签 —— 防伪造、防篡改、无需联网、可对外证明真伪。

链路：
  1) 客户机运行 `python license_admin.py fingerprint` → 得本机**机器指纹**(MachineGuid+MAC 派生)。
  2) 厂商 `python license_admin.py issue --machine <指纹> --edition pro --days 365` → 用私钥签发 `license.key`。
  3) 客户把 `license.key` 放到项目根 → 产品启动用公钥验签 + 校验指纹/有效期 → 解锁对应档位能力。

设计要点：
  * **软降级**：无 cryptography、无 license.key、或未开启强制 → 退到 `trial`（受限、限时），**绝不崩**，
    保证开发 / 自用 / 演示零摩擦（历史行为不变）。
  * **强制开关**：`AVATARHUB_LICENSE_ENFORCE=1` 才真正按 license 限制能力；默认 0 = 只评估不拦截。
  * **三档** edition：trial / standard / pro；`features` 控 HD / 多副本 / 最大并发会话 / 去水印等。
  * 仅依赖标准库 +（可选）cryptography（项目已用于 provenance）。
"""
from __future__ import annotations

import os
import sys
import json
import time
import hashlib
import platform
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _detect_base() -> Path:
    """授权状态根目录。冻结态（PyInstaller 启动器）必须用 exe 所在目录：
    __file__ 在 onefile 下指向每次运行都不同的临时解包目录——license.key/试用锚点
    写进去重启即丢（1.0.7 实锤：启动器里激活的授权一重启就失效），且与 Hub
    （跑 {app} 源码，锚在 {app}）各持一套时钟。与 app_config._detect_base 同律。"""
    env = os.environ.get("AVATARHUB_BASE", "")
    if env:
        return Path(env).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _detect_base()
LICENSE_FILE = BASE_DIR / "license.key"
PUBKEY_FILE = BASE_DIR / "license_pubkey.pem"
TRIAL_STATE_FILE = BASE_DIR / "license_state.json"   # 记录首次运行时间（试用计时锚点）
# 吊销名单（CRL）：厂商用同一私钥签名、产品用同一公钥验签的「已吊销授权」清单。
# 可选存在——缺失/未签名/被篡改 → 一律视为「无吊销」(fail-safe，绝不误伤合法授权)。
REVOCATION_FILE = Path(os.environ.get("AVATARHUB_CRL_FILE", "") or (BASE_DIR / "revocations.json"))
# 本地 CRL 缓存（在线拉取落地 + 防回滚记忆）：只增不减地记住"见过的最新 updated"，
# 即便日后被换上更旧的合法签名名单也不采信（防回滚）；放 secrets/（机器本地状态，已 gitignore）。
_CRL_CACHE_FILE = BASE_DIR / "secrets" / "revocations_cache.json"

# 内置厂商公钥（keygen 后由 license_admin 写入）。留空则回退读 license_pubkey.pem。
# 内置可防客户私换公钥来伪造；文件式便于轮换。两者皆可，内置优先。
_VENDOR_PUBKEY_PEM = """
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAJ/yPpu2EdYW5Cm2o4KojApYtw02aKRdwFkWeNLZCIuY=
-----END PUBLIC KEY-----
"""

# Ed25519（可选）：无 cryptography 则验签能力缺失 → 一律降级 trial。
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization as _ser
    _HAVE_CRYPTO = True
except Exception:
    _HAVE_CRYPTO = False

# 各档默认能力（license payload 内可显式覆盖任意键）。
EDITION_FEATURES = {
    # preset_ultra=超清1080P档 / preset_vocal=口播极致档(CodeFormer)：P4 市场化分层。
    # 仅强制模式生效（allowed() 未强制恒 True，零破坏）；旧 license.key 加载时自动并入新键默认值。
    # s2s_cloud=云端 S2S 同传后端(Seed/火山)——P0-S2S 市场化分层：旗舰版专属增值能力。
    # trial = 全功能试用（2026-07-13 产品决策）：14 天内与旗舰同权，先让用户体验完整价值，
    # 到期再收口（转正式授权按档位分层）。转化漏斗做在「体验过全部」之上，而非阉割试用。
    "trial":    {"hd": True,  "multi_replica": True,  "max_sessions": 8, "watermark_free": True,  "preset_ultra": True,  "preset_vocal": True,  "s2s_cloud": True},
    "standard": {"hd": True,  "multi_replica": False, "max_sessions": 2, "watermark_free": False, "preset_ultra": True,  "preset_vocal": False, "s2s_cloud": False},
    "pro":      {"hd": True,  "multi_replica": True,  "max_sessions": 8, "watermark_free": True,  "preset_ultra": True,  "preset_vocal": True,  "s2s_cloud": True},
}
_TRIAL_DAYS = int(os.environ.get("LICENSE_TRIAL_DAYS", "14"))


@dataclass
class LicenseState:
    valid: bool = False                 # 当前能否按该档位放行（签名有效且未过期）
    status: str = "trial"               # valid / trial / expired / mismatch / unsigned / no_crypto / revoked
    edition: str = "trial"
    machine: str = ""                   # license 绑定的机器指纹（"*"=站点授权，不绑机）
    this_machine: str = ""              # 本机指纹
    issued_ts: float = 0.0
    expires_ts: float = 0.0             # 0 = 永久
    days_left: int = -1                 # -1 = 永久 / 不适用
    grace_left: int = -1                # 宽限期剩余天（仅 status=grace 时 >=0；-1=不适用）
    features: dict = field(default_factory=dict)
    licensee: str = ""                  # 被授权方（公司/个人名，仅展示）
    message: str = ""

    # 档位/状态中文标签（单一真相：前端各页直接用 edition_label/status_label，不再各自硬编码映射）。
    EDITION_LABELS = {"trial": "试用版", "standard": "标准版", "pro": "旗舰版", "enterprise": "企业版"}
    STATUS_LABELS = {
        "valid": "已授权", "trial": "试用中", "expired": "已过期", "mismatch": "机器不符",
        "unsigned": "未签名", "no_crypto": "缺少验签库", "revoked": "已吊销", "no_module": "未授权",
        "grace": "宽限期",
    }

    def to_public(self) -> dict:
        """对外可暴露的安全视图（无私密、无密钥）。含中文标签，前后端单一真相。"""
        d = asdict(self)
        d["enforcing"] = enforcing()
        d["edition_label"] = self.EDITION_LABELS.get(self.edition, self.edition)
        d["status_label"] = self.STATUS_LABELS.get(self.status, self.status)
        # 续费临期提示（>=0 且 <=7 天）：前端据此显示橙色/续费引导，阈值集中在后端。
        d["expiring_soon"] = (self.days_left is not None and 0 <= self.days_left <= 7)
        d["in_grace"] = (self.status == "grace")   # 宽限期：已到期但仍软着陆放行，前端显强续费提示
        return d


# ── 机器指纹 ─────────────────────────────────────────────────────────
# 设计：指纹只锚定【与网络无关】的稳定标识（Windows MachineGuid / 显式 env），
# 避免换网、换网卡、虚拟网卡漂移导致合法授权一夜失配（曾发生：换网后 MAC 漂移→mismatch）。
# 兼容：匹配时同时接受旧版 GUID|MAC 指纹（machine_fingerprints 候选集），老授权平滑迁移。

def _explicit_machine_id() -> str:
    """运维显式锚点：set AVATARHUB_MACHINE_ID=<稳定串>（VM/容器/云镜像里 MachineGuid 可能易变时用）。"""
    return (os.environ.get("AVATARHUB_MACHINE_ID", "") or "").strip()


def _machine_guid() -> str:
    """Windows MachineGuid —— 与网络无关、跨重启/换网稳定，作为指纹的首选锚点。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Cryptography")
        guid, _ = winreg.QueryValueEx(k, "MachineGuid")
        winreg.CloseKey(k)
        if guid:
            return str(guid).strip()
    except Exception:
        pass
    return ""


def _node_mac() -> str:
    try:
        import uuid as _uuid
        return "%012x" % _uuid.getnode()
    except Exception:
        return ""


def _fp_from_raw(raw: str) -> str:
    """原始机器串 → 展示指纹（SHA256 取前 16 字节分 4 组：XXXX-XXXX-XXXX-XXXX）。"""
    h = hashlib.sha256(("avatarhub-fp-v1:" + raw).encode("utf-8")).hexdigest().upper()
    s = h[:16]
    return "-".join(s[i:i + 4] for i in range(0, 16, 4))


def _raw_machine_id() -> str:
    """[兼容保留] 旧版原始机器串 = MachineGuid|MAC（或 node 兜底）。
    仅用于复刻旧版指纹做候选匹配；新签发一律走 machine_fingerprint() 的稳定指纹。"""
    parts = []
    g = _machine_guid()
    if g:
        parts.append(g)
    m = _node_mac()
    if m:
        parts.append(m)   # MAC（虚拟网卡可能漂移，仅作辅助盐——已不进入主指纹）
    if not parts:
        parts.append(platform.node() or "unknown")
    return "|".join(parts)


def machine_fingerprint() -> str:
    """对外展示 / 签发用的【稳定】机器指纹。
    优先级：AVATARHUB_MACHINE_ID（显式锚点）> MachineGuid > 旧版 GUID|MAC|node 兜底。
    取稳定锚点后只用它派生，换网/换网卡不变。"""
    ex = _explicit_machine_id()
    if ex:
        return _fp_from_raw("env:" + ex)
    g = _machine_guid()
    if g:
        return _fp_from_raw(g)
    return _fp_from_raw(_raw_machine_id())


def machine_fingerprints() -> list[str]:
    """本机【可接受】的指纹候选集（授权匹配用，向后兼容旧版指纹）。
    顺序：稳定主指纹 → 旧版 GUID|MAC（精确复刻历史实现）。任一命中即视为绑定本机。
      * 新签发授权绑定主指纹 → 换网永不失配；
      * 旧版授权在 MAC 未变时仍可继续使用 → 平滑迁移、无需立刻重签。
    去重保序。"""
    cands = [machine_fingerprint()]
    legacy = _fp_from_raw(_raw_machine_id())   # 旧版 machine_fingerprint() 的逐字结果
    if legacy not in cands:
        cands.append(legacy)
    return cands


def _matches_this_machine(bound: str) -> bool:
    """bound 指纹是否匹配本机：'*' 站点授权恒真；否则命中候选集任一即可。"""
    if bound == "*":
        return True
    return bound in machine_fingerprints()


# ── 验签 ─────────────────────────────────────────────────────────────
def canonical_payload(payload: dict) -> bytes:
    """规范化序列化（签名 / 验签两端必须一致）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _load_pubkey():
    if not _HAVE_CRYPTO:
        return None
    pem = _VENDOR_PUBKEY_PEM.strip()
    if not pem:
        try:
            if PUBKEY_FILE.exists():
                pem = PUBKEY_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pem = ""
    if not pem:
        return None
    try:
        return _ser.load_pem_public_key(pem.encode("utf-8"))
    except Exception:
        return None


def verify_payload(payload: dict, sig_hex: str) -> bool:
    pk = _load_pubkey()
    if pk is None:
        return False
    try:
        pk.verify(bytes.fromhex(sig_hex), canonical_payload(payload))
        return True
    except Exception:
        return False


# ── 吊销名单 / CRL（离线可交付、可选在线分发）─────────────────────────────
# 文件结构与 license.key 同构，同一 Ed25519 密钥对签发/验签：
#   {"payload": {"v":1, "updated": ts, "revoked": [ {machine?|lic_id?|licensee?|issued?, reason?}, ... ]}, "sig": "<hex>"}
# 匹配语义：条目内 AND（列出的每个标识键都要与授权相等），名单内 OR（任一条目命中即吊销）。
#   * 按 machine+issued 可精确吊销「某机某次签发」——不会误伤日后对同机重签的新授权；
#   * 按 lic_id 可精确吊销单份（新签发已内置序列号）；按 licensee 可批量吊销某被授权方。
_REVOKE_MATCH_KEYS = ("lic_id", "machine", "licensee", "issued")


def _read_verified_crl(path) -> "tuple[int, list, dict] | None":
    """读取并【验签】单个 CRL 文件 → (updated, revoked, doc)；缺失/解析失败/签名无效 → None
    （fail-safe：无效名单绝不用于吊销，也不污染缓存）。"""
    try:
        if not path.exists():
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        payload = doc.get("payload") or {}
        if not verify_payload(payload, doc.get("sig", "")):   # 未签名/被篡改 → 忽略（不可伪造吊销）
            return None
        rev = payload.get("revoked") or []
        return int(payload.get("updated") or 0), (rev if isinstance(rev, list) else []), doc
    except Exception:
        return None


def _write_crl_cache(doc: dict) -> bool:
    """把一份已验签的 CRL 原样落地到本地缓存（原子替换）。失败静默（缓存只是加固，不是必需）。"""
    try:
        _CRL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CRL_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(_CRL_CACHE_FILE))
        return True
    except Exception:
        return False


def _load_revocations() -> list:
    """加载生效吊销名单：取【离线文件 REVOCATION_FILE 与 本地缓存】中 updated 最新者（单调防回滚）。
    * 缺失/解析失败/签名无效 → 视为无（fail-safe：绝不误伤合法授权）；
    * 文件比缓存新 → 钉入缓存（把"见过的最新"记住）；此后即便被换上更旧的合法名单也不采信（防回滚）；
    * updated 相等时以文件为准（运维当前投放的名单优先）。"""
    file_c = _read_verified_crl(REVOCATION_FILE)
    cache_c = _read_verified_crl(_CRL_CACHE_FILE)
    if file_c and (not cache_c or file_c[0] > cache_c[0]):   # 文件更新 → 单调推进缓存
        _write_crl_cache(file_c[2])
        cache_c = file_c
    cands = [c for c in (file_c, cache_c) if c]              # 文件在前：updated 相等时文件胜出
    if not cands:
        return []
    best = max(cands, key=lambda c: c[0])
    return best[1]


def _match_revoke_entry(lic_payload: dict, entry: dict) -> bool:
    """授权是否命中某吊销条目：条目里列出的每个标识键都需与授权对应字段相等（AND）。
    空条目（无任何标识键）不匹配——防止「误配置的空条目吊销一切」。"""
    if not isinstance(entry, dict):
        return False
    keys = [k for k in _REVOKE_MATCH_KEYS if entry.get(k) not in (None, "")]
    if not keys:
        return False
    return all(str(lic_payload.get(k, "")) == str(entry.get(k)) for k in keys)


def revoked_reason(lic_payload: dict, revoked: list = None):
    """授权命中吊销名单则返回原因(str)，否则 None。revoked 省略时自动加载已验签名单。"""
    if revoked is None:
        revoked = _load_revocations()
    for entry in revoked:
        if _match_revoke_entry(lic_payload, entry):
            return str(entry.get("reason", "") or "已被厂商吊销")
    return None


# ── 状态评估 ─────────────────────────────────────────────────────────
# 强制模式三档决策（2026-07-13 商业化收口）：
#   1) 环境变量显式 0/1 —— 运维/排障的最高优先级开关；
#   2) 安装目录有 license_enforce.flag —— 【客户成品机】：安装器随包铺此文件，
#      客户机默认强制（试用到期后软着陆收口）；
#   3) 都没有 —— 内部/开发机（跑源码树，无 flag）保持评估模式，零破坏。
_ENFORCE_FLAG = BASE_DIR / "license_enforce.flag"


def enforcing() -> bool:
    v = (os.environ.get("AVATARHUB_LICENSE_ENFORCE", "") or "").strip()
    if v in ("0", "1"):
        return v == "1"
    try:
        return _ENFORCE_FLAG.exists()
    except Exception:
        return False


_gen_gate_forced_at = 0.0    # 到期重算节流（宽限期内 expires_ts 恒为过去，别每请求都重算）


def generation_blocked() -> bool:
    """生成类能力是否应当整体停用（试用/授权到期后的软着陆闸门）。
    语义：只挡「继续生产新内容」，绝不挡资产读取/导出/停止/配置——
    用户的角色、声音、历史在到期后完好可取，续费即恢复。宽限期(valid=True)不挡。"""
    global _gen_gate_forced_at
    if not enforcing():
        return False
    st = load_state()
    # 长驻进程的到期时刻自愈：缓存标着「有效」但 expires_ts 已跨过 → 每分钟最多重算一次，
    # 让跨到期时刻仍在运行的 Hub 在 60s 内收口（否则 _cached 让闸门永不关闭直到重启）。
    if st.valid and st.expires_ts and time.time() > st.expires_ts \
            and time.time() - _gen_gate_forced_at > 60:
        _gen_gate_forced_at = time.time()
        st = load_state(force=True)
    return not st.valid


def blocked_message() -> str:
    """闸门命中时给前端的人话（带状态细节，各端统一文案单源）。"""
    st = load_state()
    if st.status == "expired" and st.edition == "trial":
        return (f"14 天全功能试用已结束。你的角色、声音与历史数据完好保留，"
                f"激活正式授权后立即恢复使用（控制台右上「授权」卡可自助激活）。")
    return (st.message or "授权已失效。") + " 激活/续费后立即恢复，数据不受影响。"


def _trial_first_seen() -> float:
    try:
        if TRIAL_STATE_FILE.exists():
            d = json.loads(TRIAL_STATE_FILE.read_text(encoding="utf-8"))
            fs = float(d.get("first_seen") or 0)
            if fs > 0:
                return fs
    except Exception:
        pass
    now = time.time()
    try:
        TRIAL_STATE_FILE.write_text(json.dumps({"first_seen": now}), encoding="utf-8")
    except Exception:
        pass
    return now


def _trial_state(reason: str) -> LicenseState:
    fp = machine_fingerprint()
    first = _trial_first_seen()
    exp = first + _TRIAL_DAYS * 86400
    # 向上取整：装机首日就该看到「剩 14 天」而不是 13（floor 曾让 14 天试用看着像 13 天）
    left = max(0, int((exp - time.time() + 86399) // 86400))
    expired = time.time() > exp
    feats = dict(EDITION_FEATURES["trial"])
    return LicenseState(
        valid=(not expired),
        status=("expired" if expired else "trial"),
        edition="trial", machine="", this_machine=fp,
        issued_ts=first, expires_ts=exp, days_left=left,
        features=feats, licensee="",
        message=(f"{_TRIAL_DAYS} 天全功能试用已到期，请激活正式授权（官网购买或联系客服）。{reason}" if expired
                 else f"全功能试用中：剩余 {left} 天，所有能力开放。{reason}"),
    )


_cached: LicenseState | None = None


def load_state(force: bool = False) -> LicenseState:
    global _cached
    if _cached is not None and not force:
        return _cached
    _cached = _compute_state()
    return _cached


def _compute_state() -> LicenseState:
    fp = machine_fingerprint()
    if not _HAVE_CRYPTO:
        st = _trial_state("（未安装 cryptography，无法验签）")
        st.status = "no_crypto" if st.status != "expired" else "expired"
        return st
    if not LICENSE_FILE.exists():
        return _trial_state("（未找到 license.key）")
    try:
        doc = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
        payload = doc["payload"]
        sig = doc["sig"]
    except Exception:
        st = _trial_state("（license.key 解析失败）")
        st.status = "unsigned"
        return st
    if not verify_payload(payload, sig):
        st = _trial_state("（license.key 签名无效 —— 可能被篡改或公钥不匹配）")
        st.status = "unsigned"
        st.valid = False
        return st

    edition = str(payload.get("edition", "standard"))
    bound = str(payload.get("machine", "*"))
    exp_ts = float(payload.get("expires", 0) or 0)
    issued = float(payload.get("issued", 0) or 0)
    licensee = str(payload.get("licensee", ""))
    feats = dict(EDITION_FEATURES.get(edition, EDITION_FEATURES["standard"]))
    feats.update(payload.get("features", {}) or {})

    # 指纹绑定校验（"*" = 站点授权；否则命中本机候选集任一即可，兼容旧版 GUID|MAC 指纹）
    if not _matches_this_machine(bound):
        return LicenseState(
            valid=False, status="mismatch", edition=edition, machine=bound,
            this_machine=fp, issued_ts=issued, expires_ts=exp_ts, days_left=0,
            features=feats, licensee=licensee,
            message=f"授权与本机不匹配：license 绑定 {bound}，本机 {fp}。请用本机指纹重新签发。",
        )
    # 吊销校验（厂商签名的吊销名单命中 → 立即失效，优先于到期/宽限；名单缺失/无效=不吊销）
    _rv = revoked_reason(payload)
    if _rv is not None:
        return LicenseState(
            valid=False, status="revoked", edition=edition, machine=bound,
            this_machine=fp, issued_ts=issued, expires_ts=exp_ts, days_left=0,
            features=feats, licensee=licensee,
            message=f"授权已被吊销：{_rv}。请联系厂商续期或换机。",
        )
    # 有效期校验（0 = 永久）。到期后先进入【宽限期】软着陆，宽限也过才硬失效。
    if exp_ts and time.time() > exp_ts:
        over = time.time() - exp_ts
        grace_sec = max(0, int(os.environ.get("LICENSE_GRACE_DAYS", "7"))) * 86400  # 动态读=可运行时调/可测
        if over <= grace_sec:
            # 宽限期内：仍按原档位放行（valid=True），仅标记 grace + 强提示续费——
            # 防「续费在途/周末停机」把付费客户硬切断（商用软着陆）。宽限结束→下方 expired。
            gleft = max(0, int((grace_sec - over + 86399) / 86400))   # 向上取整=剩余宽限天
            return LicenseState(
                valid=True, status="grace", edition=edition, machine=bound,
                this_machine=fp, issued_ts=issued, expires_ts=exp_ts,
                days_left=0, grace_left=gleft, features=feats, licensee=licensee,
                message=(f"授权已到期，处于宽限期（剩 {gleft} 天）。"
                         f"请尽快续费；宽限结束后高级能力将停用。"),
            )
        days_over = int(over / 86400)
        return LicenseState(
            valid=False, status="expired", edition=edition, machine=bound,
            this_machine=fp, issued_ts=issued, expires_ts=exp_ts, days_left=0,
            features=feats, licensee=licensee,
            message=f"授权已过期 {days_over} 天（到期 {time.strftime('%Y-%m-%d', time.localtime(exp_ts))}）。",
        )
    left = -1 if not exp_ts else max(0, int((exp_ts - time.time()) / 86400))
    return LicenseState(
        valid=True, status="valid", edition=edition, machine=bound,
        this_machine=fp, issued_ts=issued, expires_ts=exp_ts, days_left=left,
        features=feats, licensee=licensee,
        message=(f"已授权（{edition}）" + (f"，剩余 {left} 天。" if left >= 0 else "，永久。")),
    )


# ── 对外能力查询（被 hub / 各服务调用）──────────────────────────────────
def allowed(feature_name: str) -> bool:
    """该能力是否放行。未开启强制 → 永远 True（零破坏）。"""
    if not enforcing():
        return True
    st = load_state()
    if not st.valid:
        return False
    return bool(st.features.get(feature_name, False))


def limit(name: str, default):
    """数值额度（如 max_sessions）。未开启强制 → 返回 default（不限）。"""
    if not enforcing():
        return default
    st = load_state()
    if not st.valid:
        return EDITION_FEATURES["trial"].get(name, default)
    return st.features.get(name, default)


def preset_gate(width: int, height: int, face_enhance: str):
    """P5/P6 授权预设分层·决策核心（服务端真闸门，UI 锁可被 curl 绕过）。
    策略=降级放行而非拒绝：直播永远给画面，授权只决定画质天花板（与水印杠杆同哲学）。
    未强制 → 原样放行（零破坏）。命中降级返回 note（随响应带回给前端 toast）。
    放在 license 模块而非 hub：决策与授权状态同域，可被 _license_test.py 离线验收。
    返回 (width, height, face_enhance, note)。"""
    note = ""
    try:
        if not enforcing():
            return width, height, face_enhance, note
        if (width >= 1920 or height >= 1080) and not allowed("preset_ultra"):
            width, height = 1280, 720
            note = "超清1080P 不在当前授权档位，已按 720p 开播"
        if (face_enhance or "").strip().lower() == "codeformer" and not allowed("preset_vocal"):
            face_enhance = ""
            note = (note + "；" if note else "") + "口播极致(CodeFormer) 不在当前授权档位，已按档位默认精修"
    except Exception:
        pass
    return width, height, face_enhance, note


def effective_capabilities() -> dict:
    """在【当前 enforce + 授权状态】下，各能力此刻是否真的放行 —— 让授权闭环可观测/可测试。
    未强制 → 全放行（enforced=False，如实反映"当前不设限"）；强制 → 按 allowed()/limit() 真值。"""
    return {
        "enforced": enforcing(),
        "hd": allowed("hd"),
        "multi_replica": allowed("multi_replica"),
        "watermark_free": allowed("watermark_free"),
        "max_sessions": limit("max_sessions", None),
        "preset_ultra": allowed("preset_ultra"),
        "preset_vocal": allowed("preset_vocal"),
        "s2s_cloud": allowed("s2s_cloud"),
    }


def summary() -> dict:
    """供 /api/license/status：安全可公开视图 + 当前实际生效能力（可观测闭环）。"""
    d = load_state().to_public()          # 先定 _cached，下面 allowed/limit 复用同一状态，无递归
    d["effective"] = effective_capabilities()
    d["generation_blocked"] = generation_blocked()   # 软着陆闸门现值（前端横幅/按钮置灰单源）
    d["crl"] = _crl_status()
    # P10 授权卡激活入口：是否配置了在线激活服务器（决定兑换码提示的措辞，不泄露地址本身）
    d["activation_configured"] = activation_configured()
    d["trial_up"] = trial_up_status()     # P10 试用升级现状（active/prev_available）→ 卡上按钮显隐单源
    d["refresh_configured"] = refresh_configured()   # 在线刷新可用性（已激活+已配服务器）→「刷新授权」按钮显隐
    d["refresh"] = refresh_status()   # 最近一次刷新结果（ts/applied/age_s...）→ 后台观测「改动是否已落到本机」
    return d


def activation_configured() -> bool:
    """是否已配置在线激活服务器（兑换码可用）。未配置=仅支持粘贴 license.key 离线激活。"""
    return bool(_activation_url())


def editions_matrix() -> list:
    """P5 档位对比表（单源=EDITION_FEATURES）：授权卡渲染 试用/标准/旗舰 × 能力矩阵，
    销售话术与代码永不漂移。只含公开可说的能力位，无敏感信息。"""
    order = ["trial", "standard", "pro"]
    return [{"edition": e,
             "label": LicenseState.EDITION_LABELS.get(e, e),
             "features": dict(EDITION_FEATURES[e])} for e in order]


# ── 激活（输码 / 导入 license.key）──────────────────────────────────────
def activate_from_text(text: str) -> tuple[bool, "LicenseState | str"]:
    """从粘贴的授权码激活：接受 license.key 的 JSON 原文，或其 base64。
    校验结构 + 签名 + 机器指纹后落盘 license.key，再返回最新状态。
    返回 (True, LicenseState) 或 (False, 错误消息)。绝不抛异常。"""
    text = (text or "").strip()
    if not text:
        return False, "授权码为空。"
    doc = None
    try:
        doc = json.loads(text)
    except Exception:
        doc = None
    if doc is None:
        try:
            import base64
            doc = json.loads(base64.b64decode(text).decode("utf-8"))
        except Exception:
            return False, "授权码格式无法识别（应为 license.key 内容，或其 base64）。"
    if not isinstance(doc, dict) or "payload" not in doc or "sig" not in doc:
        return False, "授权码结构不完整（缺 payload / sig）。"
    if not _HAVE_CRYPTO:
        return False, "本机缺少 cryptography 验签库，无法激活。"
    if not verify_payload(doc["payload"], doc["sig"]):
        return False, "签名校验失败（可能被篡改 / 损坏，或与厂商公钥不匹配）。"
    bound = str(doc["payload"].get("machine", "*"))
    fp = machine_fingerprint()
    if not _matches_this_machine(bound):
        return False, f"该授权绑定机器 {bound}，与本机 {fp} 不符。请用本机指纹重新签发。"
    try:
        LICENSE_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return False, f"写入 license.key 失败：{e}"
    return True, load_state(force=True)


def activate_from_file(path) -> tuple[bool, "LicenseState | str"]:
    """从 license.key 文件激活（导入按钮用）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return False, f"读取文件失败：{e}"
    return activate_from_text(text)


# ── 在线激活（输兑换码，自助换 license）────────────────────────────────
# 默认在线激活服务器（官网订单后端）：1.0.9 起客户端开箱即可「输订单号在线激活」，
# 无需配置。白标/私有部署可用 AVATARHUB_ACTIVATION_URL 或 config.json.activation_url 覆盖，
# 置 "off"/"none" 可显式关闭（退回纯离线粘贴授权码）。
_DEFAULT_ACTIVATION_URL = "https://usdt2026.cc"


def _activation_url(explicit: str = "") -> str:
    """激活服务器地址：显式参数 > 环境变量 AVATARHUB_ACTIVATION_URL > config.json.activation_url
    > 内置默认（官网订单后端）。返回 "" = 未配置（仅离线粘贴激活）。"""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("AVATARHUB_ACTIVATION_URL", "").strip()
    if env:
        return "" if env.lower() in ("off", "none", "0") else env
    try:
        cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
        u = (cfg.get("activation_url") or "").strip()
        if u:
            return "" if u.lower() in ("off", "none", "0") else u
    except Exception:
        pass
    return _DEFAULT_ACTIVATION_URL


def activate_online(code: str, server_url: str = "", timeout: float = 20.0
                    ) -> tuple[bool, "LicenseState | str"]:
    """输兑换码在线激活：POST {code, fingerprint} 到激活服务器换取已签授权，
    再走本地验签 + 指纹校验 + 落盘（纵深防御：即便服务器签发，本地仍用内置公钥复验）。
    返回 (True, LicenseState) 或 (False, 错误消息)。绝不抛异常。"""
    code = (code or "").strip()
    if not code:
        return False, "兑换码为空。"
    url = _activation_url(server_url)
    if not url:
        return False, "未配置激活服务器地址（设环境变量 AVATARHUB_ACTIVATION_URL 或 config.json.activation_url）。"
    if not _HAVE_CRYPTO:
        return False, "本机缺少 cryptography 验签库，无法激活。"
    import urllib.request
    import urllib.error
    endpoint = url.rstrip("/") + "/api/activate"
    body = json.dumps({"code": code, "fingerprint": machine_fingerprint()}).encode("utf-8")
    try:
        req = urllib.request.Request(endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            resp = json.loads(e.read().decode("utf-8"))
        except Exception:
            resp = {}
        return False, resp.get("error") or f"激活服务器返回 HTTP {e.code}。"
    except Exception as e:
        return False, f"连接激活服务器失败：{e}"
    if not isinstance(resp, dict) or not resp.get("ok") or "license" not in resp:
        return False, (resp.get("error") if isinstance(resp, dict) else None) or "激活失败（服务器未返回授权）。"
    return activate_from_text(json.dumps(resp["license"], ensure_ascii=False))


# ── 在线刷新（管理后台续费/升档/改能力 → 客户端按指纹「只升不降」拉取生效）────────────
# 与 activate_online 的关键差异：
#   1) 无需重新输码——【机器指纹即身份】，后台按指纹查当前应得授权重签回传（无新增待泄露的秘密）；
#   2) 【单调闸门】：背景刷新绝不把本地更优的有效授权改差（防后台在途/回滚/误配返回更短或降档时
#      把好授权覆盖坏）。用户显式的手动激活(activate_online/from_text)不受此限——显式意图优先。
# 这补上了授权闭环缺的「正向拉取」（此前只有 fetch_crl_online 的负向吊销拉取）。
_EDITION_RANK = {"trial": 0, "standard": 1, "pro": 2, "enterprise": 2}


def _eff_expiry(expires) -> float:
    """比较用有效到期：0(永久) 视为 +inf，便于「永久 > 任何有限期」的单调判断。"""
    e = float(expires or 0)
    return float("inf") if e == 0 else e


def _doc_is_upgrade(new_payload: dict, cur: "LicenseState") -> bool:
    """候选授权相对「当前状态」是否更优（背景刷新的单调闸门核心）。
    规则（保守，绝不在后台悄悄降能力）：
      * 当前无有效授权 → 任何本机有效签名授权都更优（续命/恢复）；
      * 档位更高 → 更优（升档，不看到期）；
      * 档位相同且到期更晚(>1h 容差防签发时刻抖动) → 更优（续费）；
      * 其余（同档不更晚 / 档位更低即便更长）→ 不更优（跳过，不覆盖、不写盘）。"""
    if not cur.valid:
        return True
    rank = _EDITION_RANK.get(str(new_payload.get("edition", "")), -1)
    cur_rank = _EDITION_RANK.get(cur.edition, -1)
    if rank > cur_rank:
        return True
    if rank == cur_rank and _eff_expiry(new_payload.get("expires")) > _eff_expiry(cur.expires_ts) + 3600:
        return True
    return False


def _refresh_online_impl(server_url: str = "", timeout: float = 20.0
                         ) -> tuple[bool, "LicenseState | str"]:
    """refresh_online 的实现体（网络+验签+单调落盘）；对外统一入口是下面的 refresh_online 包装
    （包装负责节流锚与可观测落盘，全部结果都过一次记录）。"""
    url = _activation_url(server_url)
    if not url:
        return False, "未配置激活服务器地址。"
    if not _HAVE_CRYPTO:
        return False, "本机缺少 cryptography 验签库。"
    cur = load_state()
    cur_doc = _read_license_doc() or {}
    lic_id = str((cur_doc.get("payload") or {}).get("lic_id", "") or "")
    import urllib.request
    import urllib.error
    endpoint = url.rstrip("/") + "/api/refresh"
    body = json.dumps({"fingerprint": machine_fingerprint(), "lic_id": lic_id}).encode("utf-8")
    try:
        req = urllib.request.Request(endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            resp = json.loads(e.read().decode("utf-8"))
        except Exception:
            resp = {}
        return False, resp.get("error") or f"刷新服务器返回 HTTP {e.code}。"
    except Exception as e:
        return False, f"连接刷新服务器失败：{e}"
    if not isinstance(resp, dict) or not resp.get("ok") or "license" not in resp:
        return False, (resp.get("error") if isinstance(resp, dict) else None) or "刷新失败（服务器未返回授权）。"
    doc = resp["license"]
    payload = (doc or {}).get("payload") or {}
    if not verify_payload(payload, (doc or {}).get("sig", "")):
        return False, "刷新返回的授权签名无效（已忽略，不可伪造）。"
    if not _matches_this_machine(str(payload.get("machine", "*"))):
        return False, "刷新返回的授权与本机指纹不符（已忽略）。"
    if not _doc_is_upgrade(payload, cur):
        return False, "已是最新授权，无需更新。"
    return activate_from_text(json.dumps(doc, ensure_ascii=False))


def refresh_configured() -> bool:
    """本机是否具备「在线刷新」条件：已激活(有 license.key)且已配置激活服务器。
    供 hub 决定是否启用周期刷新、供前端决定「刷新授权」按钮显隐。
    纯试用/开发机(无 key)不刷新——没有可续的东西，也不该无故联网。"""
    try:
        return bool(_activation_url()) and LICENSE_FILE.exists()
    except Exception:
        return False


# ── 刷新可观测 + 节流（管理后台/运维要能看到「这台机器最近拉过没、拉到了什么」；
#    并让后台周期刷新与「被闸门挡时按需 JIT 刷新」共用一个节流窗口，避免热点旁路狂刷）────────
_LAST_REFRESH_FILE = BASE_DIR / "secrets" / "license_refresh.json"
_last_refresh_attempt = 0.0     # 进程内节流锚（周期循环与按需 JIT 共用同一窗口）


def _record_refresh(applied: bool, detail: str, st: "LicenseState | None" = None):
    """把一次刷新结果落到本地可审计文件（best-effort，写失败静默，绝不影响授权）。"""
    try:
        rec = {"ts": int(time.time()), "applied": bool(applied), "detail": str(detail)[:200]}
        if st is not None:
            rec.update({"status": st.status, "edition": st.edition,
                        "expires_ts": st.expires_ts, "days_left": st.days_left})
        _LAST_REFRESH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LAST_REFRESH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(_LAST_REFRESH_FILE))
    except Exception:
        pass


def refresh_status() -> dict:
    """最近一次在线刷新的结果（供 summary/看板/运维观测「后台改动是否已落到本机」）。
    附 age_s=距今秒数（前端可显示「N 分钟前刷新」）。无记录 → {}。"""
    try:
        if _LAST_REFRESH_FILE.exists():
            d = json.loads(_LAST_REFRESH_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d["age_s"] = max(0, int(time.time() - int(d.get("ts", 0) or 0)))
                return d
    except Exception:
        pass
    return {}


def refresh_due(min_interval_s: float = 300) -> bool:
    """是否到了可再刷新的节流窗口（已配置刷新 + 距上次尝试 >= min_interval）。
    便宜的前置判断：供「状态查询发现失效」等热点旁路仅在真该刷时才起后台线程，不每拍狂刷。"""
    return refresh_configured() and (time.time() - _last_refresh_attempt >= max(0, min_interval_s))


def refresh_online(server_url: str = "", timeout: float = 20.0
                   ) -> tuple[bool, "LicenseState | str"]:
    """按机器指纹在线刷新授权：管理后台改了本机授权(续费/升档/改能力)后，客户端拉取并「只升不降」落盘。
    POST {fingerprint, lic_id?} → {activation_url}/api/refresh 取回已签授权，
    本地内置公钥复验签名 + 校验指纹（不盲信服务器）后，仅当【比当前更优】才落盘。
    统一在此记录节流锚 + 落盘可观测结果（成功/跳过/失败都记一次）。
    返回：(True, LicenseState)=已应用更优授权 / (False, 说明串)。绝不抛异常。
    与吊销拉取一致的离线优先：未配置激活地址→直接跳过，绝不因联网失败影响既有授权。"""
    global _last_refresh_attempt
    _last_refresh_attempt = time.time()
    ok, res = _refresh_online_impl(server_url, timeout)
    _record_refresh(ok, ("已应用更优授权" if ok else str(res)),
                    res if not isinstance(res, str) else load_state())
    return ok, res


def maybe_refresh(min_interval_s: float = 300, force: bool = False) -> dict:
    """按需刷新（节流）：被闸门挡 / 状态查询发现失效时即时补拉，免得干等后台周期(默认1h)。
    force=True 忽略节流（手动按钮）。返回 {ok, applied?, skipped?, detail}。绝不抛异常。"""
    if not force and not refresh_due(min_interval_s):
        return {"ok": False, "applied": False, "skipped": "throttled_or_unconfigured"}
    ok, res = refresh_online()
    return {"ok": bool(ok), "applied": bool(ok),
            "detail": (res if isinstance(res, str) else getattr(res, "message", ""))}


# ── P10 一键试用升级（客户端半场）────────────────────────────────────────
# 服务器 /api/trial_upgrade 按指纹签发限时旗舰试授权（一机一次、有效期内幂等续领、到期拒发）。
# 客户端职责：①有效正式授权在手→直接拒绝（试签限时，绝不能把正式授权降级覆盖）；
# ②覆盖前若有旧 key（宽限/过期/失配的正式授权）先备份，可一键还原；
# ③领回的试签仍走 activate_from_text 全套复验（签名+指纹），服务器不被盲信。
TRIAL_PREV_FILE = BASE_DIR / "license_prev.key"


def is_trial_upgrade_doc(doc) -> bool:
    """该 license 文档是否为「试用升级」签发（lic_id 前缀约定，随签名不可伪造）。"""
    try:
        return str((doc or {}).get("payload", {}).get("lic_id", "")).startswith("trial-")
    except Exception:
        return False


def _read_license_doc():
    try:
        return json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def trial_up_status() -> dict:
    """试用升级可观测视图（授权卡按钮/门禁断言用）：
    active=当前 key 是试签且仍有效；prev_available=有可还原的正式授权备份。"""
    doc = _read_license_doc()
    return {"active": bool(is_trial_upgrade_doc(doc) and load_state().valid),
            "prev_available": TRIAL_PREV_FILE.exists()}


def _backup_before_trial() -> bool:
    """试签覆盖前备份现有正式授权。只备份「验签通过且非试签」的 key——
    重复点试用不会拿试签把真备份冲掉；无效/伪造 key 不值得备份。"""
    try:
        doc = _read_license_doc()
        if doc and not is_trial_upgrade_doc(doc) \
                and verify_payload(doc.get("payload", {}), doc.get("sig", "")):
            TRIAL_PREV_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            return True
    except Exception:
        pass
    return False


def trial_upgrade_online(server_url: str = "", timeout: float = 20.0
                         ) -> tuple[bool, "LicenseState | str"]:
    """一键试用升级：POST {fingerprint} 到激活服务器 /api/trial_upgrade 换限时 pro 试签。
    与 activate_online 同一纵深防御（本地复验签名+指纹后才落盘）。绝不抛异常。
    护栏：已持有效【旗舰】正式授权时拒绝（试签没有增量价值，反而把永久 key 换成限时的）；
    标准/试用档放行——覆盖前 _backup_before_trial 备份正式授权，试完 trial_restore 一键还原。"""
    cur = load_state(force=True)
    if cur.valid and cur.status == "valid" and cur.edition == "pro" \
            and not is_trial_upgrade_doc(_read_license_doc()):
        label = LicenseState.EDITION_LABELS.get(cur.edition, cur.edition)
        return False, f"本机已持有效授权（{label}），无需试用升级。"
    url = _activation_url(server_url)
    if not url:
        return False, "未配置激活服务器地址（设环境变量 AVATARHUB_ACTIVATION_URL 或 config.json.activation_url）。"
    if not _HAVE_CRYPTO:
        return False, "本机缺少 cryptography 验签库，无法激活。"
    import urllib.request
    import urllib.error
    endpoint = url.rstrip("/") + "/api/trial_upgrade"
    body = json.dumps({"fingerprint": machine_fingerprint()}).encode("utf-8")
    try:
        req = urllib.request.Request(endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            resp = json.loads(e.read().decode("utf-8"))
        except Exception:
            resp = {}
        return False, resp.get("error") or f"激活服务器返回 HTTP {e.code}。"
    except Exception as e:
        return False, f"连接激活服务器失败：{e}"
    if not isinstance(resp, dict) or not resp.get("ok") or "license" not in resp:
        return False, (resp.get("error") if isinstance(resp, dict) else None) or "试用申请失败（服务器未返回授权）。"
    _backup_before_trial()
    return activate_from_text(json.dumps(resp["license"], ensure_ascii=False))


def trial_restore() -> tuple[bool, "LicenseState | str"]:
    """还原试用升级前备份的正式授权（activate_from_file 全套复验，备份被篡改不落盘）。
    还原成功即清备份文件——授权卡按钮随之消失，状态收敛。"""
    if not TRIAL_PREV_FILE.exists():
        return False, "没有可还原的正式授权备份。"
    ok_, res = activate_from_file(TRIAL_PREV_FILE)
    if ok_:
        try:
            TRIAL_PREV_FILE.unlink()
        except Exception:
            pass
    return ok_, res


def trial_autorestore_if_due() -> "str | None":
    """P11 试用到期软着陆：当前 key 是**已到期/进宽限**的试签且有正式授权备份 → 自动还原，
    免得客户对着「已过期」红横幅手足无措（正式授权明明还在手里）。
    试签到期不该吃宽限期——宽限是给付费授权「续费在途」的软着陆，试用到点就该收。
    只在明确「试签+到期+有备份」三条件齐时动手；还原走 trial_restore 全套复验。
    返回描述串（发生了还原）或 None（无事发生）。绝不抛异常。"""
    try:
        doc = _read_license_doc()
        if not (doc and is_trial_upgrade_doc(doc) and TRIAL_PREV_FILE.exists()):
            return None
        exp = float((doc.get("payload") or {}).get("expires", 0) or 0)
        if not exp or time.time() <= exp:
            return None
        ok_, res = trial_restore()
        if ok_:
            label = LicenseState.EDITION_LABELS.get(res.edition, res.edition)
            return f"旗舰试用已到期，已自动还原正式授权（{label}）"
        return None
    except Exception:
        return None


# ── 吊销名单在线拉取 / 时效防护（可选，离线优先）────────────────────────────
def _crl_url(explicit: str = "") -> str:
    """CRL 分发地址：显式 > 环境变量 AVATARHUB_CRL_URL > 激活服务器(_activation_url)/api/revocations。
    未配置 → ""（离线优先：不联网，只吃本地文件/缓存）。"""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("AVATARHUB_CRL_URL", "").strip()
    if env:
        return env
    base = _activation_url()
    return (base.rstrip("/") + "/api/revocations") if base else ""


def crl_source_configured() -> bool:
    """是否已配置在线 CRL 源（供 hub 决定是否启用周期拉取；未配置=离线优先，零联网）。"""
    return bool(_crl_url())


def fetch_crl_online(url: str = "", timeout: float = 8.0) -> dict:
    """可选在线拉取吊销名单（best-effort）：GET 已签 CRL → 内置公钥验签 → 仅当【比本地缓存新】才落地
    （单调防回滚）。落地后置 _cached=None，下次 load_state 即时重算，新吊销分钟级生效。
    离线优先：未配置地址 → 直接跳过。绝不抛异常，绝不因联网失败影响既有状态。
    返回 {ok, applied, updated?, source?, error?/note?}。"""
    endpoint = _crl_url(url)
    if not endpoint:
        return {"ok": False, "applied": False, "error": "未配置 CRL 地址（AVATARHUB_CRL_URL 或激活服务器）。"}
    if not _HAVE_CRYPTO:
        return {"ok": False, "applied": False, "error": "本机缺少 cryptography，无法验签 CRL。"}
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(endpoint, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "applied": False, "source": endpoint, "error": f"CRL 服务器返回 HTTP {e.code}。"}
    except Exception as e:
        return {"ok": False, "applied": False, "source": endpoint, "error": f"拉取 CRL 失败：{e}"}
    payload = doc.get("payload") if isinstance(doc, dict) else None
    if not isinstance(payload, dict) or not verify_payload(payload, doc.get("sig", "")):
        return {"ok": False, "applied": False, "source": endpoint, "error": "CRL 签名无效（已忽略，不可伪造吊销）。"}
    updated = int(payload.get("updated") or 0)
    cache_c = _read_verified_crl(_CRL_CACHE_FILE)
    if cache_c and updated <= cache_c[0]:                     # 不比本地新 → 不动（防回滚）
        return {"ok": True, "applied": False, "updated": updated, "source": endpoint,
                "note": "远端不比本地新，未更新。"}
    if not _write_crl_cache(doc):
        return {"ok": False, "applied": False, "source": endpoint, "error": "CRL 缓存写入失败。"}
    global _cached
    _cached = None                                            # 令下次 load_state 重算 → 新吊销即时生效
    return {"ok": True, "applied": True, "updated": updated, "source": endpoint}


def _crl_status() -> dict:
    """当前生效 CRL 的可观测视图（供 /api/license/status 与安全体检）：
    是否存在 / 条目数 / 更新时间 / 陈旧天数 / 来源(file|cache) / 是否已配在线源。"""
    file_c = _read_verified_crl(REVOCATION_FILE)
    cache_c = _read_verified_crl(_CRL_CACHE_FILE)
    cands = [c for c in (file_c, cache_c) if c]
    online = bool(_crl_url())
    if not cands:
        return {"present": False, "count": 0, "updated": 0, "age_days": None,
                "source": "none", "online": online}
    best = max(cands, key=lambda c: c[0])
    updated = best[0]
    age_days = max(0, int((time.time() - updated) / 86400)) if updated else None
    source = "file" if (file_c and best[0] == file_c[0]) else "cache"
    return {"present": True, "count": len(best[1]), "updated": updated,
            "age_days": age_days, "source": source, "online": online}


if __name__ == "__main__":
    st = load_state(force=True)
    print(json.dumps(st.to_public(), ensure_ascii=False, indent=2))
