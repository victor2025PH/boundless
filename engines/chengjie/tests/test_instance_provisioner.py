"""Sprint5：实例开通规划器（端口分配防冲突 / overlay / 幂等 stack 登记）。"""
import pytest

from src.ops.instance_provisioner import (
    ALT_OFFSET,
    RESERVED_PORTS,
    allocate_ports,
    build_stack_entry,
    launch_command,
    plan_instance,
    render_overlay,
    slugify,
    upsert_stack_entry,
    used_ports_from_stack,
)


def _stack():
    """仿真现网：智聊 18799/18787 + 通译 18899/18887。"""
    return {
        "version": 1,
        "services": [
            {"id": "chengjie_zhiliao", "ports": [18799, 18787]},
            {"id": "chengjie_tongyi", "ports": [18899, 18887]},
            {"id": "huoke", "ports": [18080, 8000]},
            {"id": "website", "ports": [3000]},
        ],
    }


def test_slugify():
    assert slugify("Acme Ltd.") == "acme_ltd"
    assert slugify("  ") == "cust"
    assert slugify("客户A") == "a" or slugify("客户A") == "cust"  # 非 ascii 被清


def test_used_ports_collects_all():
    used = used_ports_from_stack(_stack())
    assert {18799, 18787, 18899, 18887, 18080, 8000, 3000} <= used


def test_allocate_ports_avoids_used_and_reserved():
    used = used_ports_from_stack(_stack())
    web, alt, met = allocate_ports("zhiliao", used)
    # 18799(k0)/18899(k1,通译占) 均被跳过 → 首个空档 k=2：web 18999 / met 19200+2*100
    assert web == 18999
    assert alt == web - ALT_OFFSET == 18987
    assert met == 19400
    for p in (web, alt, met):
        assert p not in used and p not in RESERVED_PORTS
    assert len({web, alt, met}) == 3


def test_allocate_ports_second_customer_no_collision():
    st = _stack()
    # 追加第一个客户实例后再分配第二个 → 端口必须再次错开
    p1 = plan_instance(st, product="zhiliao", customer="acme")
    st["services"].append({"id": p1.service_id, "ports": [p1.web_port, p1.alt_port]})
    p2 = plan_instance(st, product="zhiliao", customer="beta")
    assert p2.web_port != p1.web_port
    assert p2.web_port == 19099 and p2.alt_port == 19087


def test_plan_instance_fields():
    p = plan_instance(_stack(), product="zhiliao", customer="Acme Ltd")
    assert p.instance_id == "zhiliao_acme_ltd"
    assert p.service_id == "chengjie_zhiliao_acme_ltd"
    assert p.product_id == "zhiliao"
    assert p.data_dir.endswith(r"\zhiliao_acme_ltd\data")
    assert p.web_port == 18999


def test_render_overlay_has_port_brand_secrets():
    p = plan_instance(_stack(), product="zhiliao", customer="acme")
    ov = render_overlay(p, secret_key="SK", auth_token="TOK")
    assert "port: 18999" in ov
    assert "product_name: 智聊 ChatX" in ov
    assert "secret_key: SK" in ov and "auth_token: TOK" in ov
    assert "monitoring:\n  enabled: false" in ov


def test_render_overlay_random_secrets_differ():
    p = plan_instance(_stack(), product="zhiliao", customer="acme")
    a = render_overlay(p)
    b = render_overlay(p)
    assert a != b  # 每次随机 secret/token


def test_build_stack_entry_shape():
    p = plan_instance(_stack(), product="zhiliao", customer="acme")
    e = build_stack_entry(p)
    assert e["id"] == "chengjie_zhiliao_acme"
    assert e["ports"] == [18999, 18987]
    assert e["enabled"] is False
    assert "-InstanceId zhiliao_acme" in e["up"]["args"]
    assert "-Port 18999" in e["up"]["args"]
    assert "-ProductId zhiliao" in e["up"]["args"]
    assert e["up"]["script"] == "start_zhiliao.ps1"


def test_upsert_stack_entry_idempotent():
    st = _stack()
    p = plan_instance(st, product="zhiliao", customer="acme")
    e = build_stack_entry(p)
    st, act1 = upsert_stack_entry(st, e)
    assert act1 == "added"
    n_after = len(st["services"])
    st, act2 = upsert_stack_entry(st, e)   # 再登记同 id
    assert act2 == "exists"
    assert len(st["services"]) == n_after  # 不重复追加


def test_launch_command_contains_params():
    p = plan_instance(_stack(), product="zhiliao", customer="acme")
    cmd = launch_command(p)
    assert "start_zhiliao.ps1" in cmd
    assert "-InstanceId zhiliao_acme" in cmd and "-Port 18999" in cmd
    assert "-ProductId zhiliao" in cmd and r"\zhiliao_acme\data" in cmd


def test_unknown_product_rejected():
    with pytest.raises(ValueError):
        plan_instance(_stack(), product="matrixx", customer="x")
