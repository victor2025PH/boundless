"""按订单准备一个新 chengjie 实例（自助下单自动开通的确定性半程）。

Sprint5：把「开新客户实例」里**确定性、易错**的部分自动化——分配 instance_id / 端口 /
数据根、渲染 config.local.yaml overlay、幂等登记 deploy/stack.json、生成拉起命令。纯逻辑，
可单测；**不**拉起进程（交 start_zhiliao.ps1）、**不**签发 license（交 fulfill_chatx）、
**不**建 junction / 登录 session / 配防火墙（交 PowerShell / 人工）。

端口分配：产品基址 + k*100，避开 stack.json 已占用 + 保留端口（README 占用表），杜绝与
现有实例端口冲突（否则 watchdog/status 探活会互相误判）。数据根：D:\\chengjie-instances\\<id>\\data。
"""
from __future__ import annotations

import re
import secrets as _secrets
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 产品 web 主端口基址（deploy/instances/README §端口表）
PRODUCT_WEB_BASE = {"zhiliao": 18799, "tongyi": 18899}
# metrics 端口基址（避开 example 默认 19190 / 现网 19199）
METRICS_BASE = 19200
# 主/备端口差（沿用 18799↔18787 的既有模式）
ALT_OFFSET = 12
# README「已占用参照（勿再分配）」+ 现网 metrics
RESERVED_PORTS = {
    18080, 8000, 3000, 7899, 9000, 7852, 7854, 7857, 7858, 11434, 19190, 19199,
}
# 生产数据根基址（status_instances.ps1 $ProdBase 同源）
PROD_DATA_BASE = r"D:\chengjie-instances"

# 产品线遥测 product_id（CHENGJIE_PRODUCT_ID）——客户实例仍归属产品线，不是 per-customer
PRODUCT_TELEMETRY_ID = {"zhiliao": "zhiliao", "tongyi": "tongyi"}
PRODUCT_BRAND = {
    "zhiliao": {"product_name": "智聊 ChatX", "site_name": "无界科技 · 智聊"},
    "tongyi": {"product_name": "通译 LingoX", "site_name": "无界科技 · 通译"},
}


@dataclass
class InstancePlan:
    instance_id: str            # 如 zhiliao_acme（数据根/日志/服务 id 用）
    service_id: str             # stack.json services[].id：chengjie_<instance_id>
    product: str                # zhiliao / tongyi
    product_id: str             # 遥测 CHENGJIE_PRODUCT_ID（=product）
    data_dir: str               # D:\chengjie-instances\<instance_id>\data
    web_port: int
    alt_port: int
    metrics_port: int
    product_name: str           # 品牌展示名（可被 customer brand 覆盖）
    customer: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_id": self.instance_id, "service_id": self.service_id,
            "product": self.product, "product_id": self.product_id,
            "data_dir": self.data_dir, "web_port": self.web_port,
            "alt_port": self.alt_port, "metrics_port": self.metrics_port,
            "product_name": self.product_name, "customer": self.customer,
        }


def slugify(name: str) -> str:
    """客户名 → 安全 slug（小写字母数字下划线，限长）。空 → 'cust'。"""
    s = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    return (s or "cust")[:32]


def used_ports_from_stack(stack: Dict[str, Any]) -> set:
    """收集 stack.json 所有 services[].ports，作为已占用端口集合。"""
    used: set = set()
    for svc in (stack or {}).get("services", []) or []:
        for p in svc.get("ports", []) or []:
            try:
                used.add(int(p))
            except (TypeError, ValueError):
                continue
    return used


def allocate_ports(product: str, used: set) -> Tuple[int, int, int]:
    """分配 (web, alt, metrics)，避开 used ∪ RESERVED，且三者互不相同。

    产品基址 + k*100 逐档探测；k 增长跳过与现有实例/保留端口冲突的档位。
    """
    if product not in PRODUCT_WEB_BASE:
        raise ValueError(f"未知产品: {product!r}（支持 {sorted(PRODUCT_WEB_BASE)}）")
    base = PRODUCT_WEB_BASE[product]
    blocked = set(used) | set(RESERVED_PORTS)
    for k in range(0, 100):
        web = base + k * 100
        alt = web - ALT_OFFSET
        met = METRICS_BASE + k * 100
        cand = {web, alt, met}
        if len(cand) < 3:
            continue
        if cand & blocked:
            continue
        return web, alt, met
    raise RuntimeError(f"为 {product} 找不到空闲端口档（已试 100 档）")


def plan_instance(
    stack: Dict[str, Any],
    *,
    product: str,
    customer: str,
    instance_id: Optional[str] = None,
    data_base: str = PROD_DATA_BASE,
    product_name: Optional[str] = None,
) -> InstancePlan:
    """据现有 stack + 订单，规划一个新实例（不写盘、不改 stack）。"""
    if product not in PRODUCT_WEB_BASE:
        raise ValueError(f"未知产品: {product!r}")
    iid = instance_id or f"{product}_{slugify(customer)}"
    used = used_ports_from_stack(stack)
    web, alt, met = allocate_ports(product, used)
    brand = PRODUCT_BRAND.get(product, {})
    return InstancePlan(
        instance_id=iid,
        service_id=f"chengjie_{iid}",
        product=product,
        product_id=PRODUCT_TELEMETRY_ID.get(product, product),
        data_dir=f"{data_base}\\{iid}\\data",
        web_port=web, alt_port=alt, metrics_port=met,
        product_name=product_name or brand.get("product_name", product),
        customer=customer,
    )


def render_overlay(
    plan: InstancePlan,
    *,
    host: str = "127.0.0.1",
    secret_key: Optional[str] = None,
    auth_token: Optional[str] = None,
    enable_monitoring: bool = False,
    site_name: Optional[str] = None,
) -> str:
    """渲染该实例的 config.local.yaml（品牌 + 端口 + 机密断言）。

    新客户实例是全新初始化（非迁移），故默认**生成随机** secret_key/auth_token 写入 overlay
    （与迁移场景不同——迁移时机密留在现网 config.yaml，模板不携带）。
    """
    sk = secret_key or _secrets.token_urlsafe(32)
    tok = auth_token or _secrets.token_urlsafe(24)
    _site = site_name or f"无界科技 · {plan.product_name}"
    lines: List[str] = [
        f"# {plan.product_name} 实例 overlay（provision_instance 生成；instance_id={plan.instance_id}）",
        f"# 客户: {plan.customer or '-'}    数据根: {plan.data_dir}",
        "# 机密（secret_key/auth_token）随实例随机生成；请妥善保管，勿回写模板/入库。",
        "web_admin:",
        "  enabled: true",
        f"  host: {host}",
        f"  port: {plan.web_port}",
        f"  site_name: {_site}",
        f"  secret_key: {sk}",
        f"  auth_token: {tok}",
        "  cookie_secure: false   # 经 TLS 反代对外时改 true",
        "brand:",
        "  company_name: 无界科技",
        f"  product_name: {plan.product_name}",
        f"  site_name: {_site}",
        "monitoring:",
        f"  enabled: {'true' if enable_monitoring else 'false'}",
    ]
    if enable_monitoring:
        lines.append(f"  metrics_port: {plan.metrics_port}")
    lines.append("")
    return "\n".join(lines)


def build_stack_entry(plan: InstancePlan) -> Dict[str, Any]:
    """构造 stack.json 的一个 service 条目（默认 enabled=false，人工观察期后再开）。"""
    return {
        "id": plan.service_id,
        "title": f"{plan.product_name} · 客户实例 {plan.customer or plan.instance_id}",
        "group": "engine",
        "profiles": ["chengjie-dual"],
        "dir": "deploy/instances",
        "runtime": "python",
        "entry": "main.py",
        "ports": [plan.web_port, plan.alt_port],
        "health": {
            "url": f"http://127.0.0.1:{plan.web_port}/api/admin/health",
            "regex": "", "auth": True,
        },
        "gpu": False,
        "up": {
            "via": "script", "script": "start_zhiliao.ps1",
            "args": (f"-InstanceId {plan.instance_id} -Port {plan.web_port} "
                     f"-ProductId {plan.product_id} -DataDir \"{plan.data_dir}\""),
        },
        "down": {"via": "port"},
        "enabled": False,
        "notes": (f"[provision_instance 生成] 客户实例；数据根 {plan.data_dir}；"
                  f"端口 {plan.web_port}/{plan.alt_port}。license 由 fulfill_chatx 签发后粘贴激活；"
                  f"启用前跑 preflight/verify，并按需扩展 status/watchdog 识别本 id。"),
    }


def upsert_stack_entry(
    stack: Dict[str, Any], entry: Dict[str, Any]
) -> Tuple[Dict[str, Any], str]:
    """幂等登记：按 service id 存在则跳过（返回 'exists'），否则追加（'added'）。不覆盖既有条目。"""
    services = stack.setdefault("services", [])
    for svc in services:
        if svc.get("id") == entry.get("id"):
            return stack, "exists"
    services.append(entry)
    return stack, "added"


def launch_command(plan: InstancePlan) -> str:
    """生成拉起命令（交 PowerShell 执行；provision 本身不拉起）。"""
    return (
        "powershell -NoProfile -ExecutionPolicy Bypass -File "
        "deploy\\instances\\start_zhiliao.ps1 "
        f"-InstanceId {plan.instance_id} -Port {plan.web_port} "
        f"-ProductId {plan.product_id} -DataDir \"{plan.data_dir}\""
    )
