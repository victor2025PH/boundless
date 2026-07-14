# -*- coding: utf-8 -*-
"""P1-3 明星命名合规迁移：真人名角色 → 中性描述名（民法典 1023 条声音/姓名权风险）。

- 走 Hub PATCH /profiles/{name} 的 new_name 通道（DB 单事务 + 激活指针跟随 + 广播，零手搓）。
- 别名映射存 legacy_alias_map.json（内部可溯源，对外界面只见新名）。
- refs/interp_<旧名>.{wav,txt} 参考音缓存同步改名（不改也能用，只是回退全长参考重预热，白等几秒）。
"""
import io, json, os, sys
import requests

HUB = "http://127.0.0.1:9000"
BASE = r"C:\模仿音色"
MAP_FILE = os.path.join(BASE, "legacy_alias_map.json")
REFS = os.path.join(BASE, "refs")

RENAMES = {
    "刘德华":       "磁性港风",
    "古天乐":       "沉稳港风",
    "刘亦菲":       "清雅淑女",
    "杰森斯坦森":   "硬汉先生",
    "皮特":         "欧美绅士",
    "葛优":         "京味幽默",
    "彭于晏":       "阳光型男",
    "菲律宾马斯克": "海外总裁",
}


def main():
    existing = {p["name"] for p in requests.get(f"{HUB}/profiles", timeout=10).json()["profiles"]}
    alias = {}
    if os.path.exists(MAP_FILE):
        alias = json.load(io.open(MAP_FILE, encoding="utf-8"))
    done, skipped = [], []
    for old, new in RENAMES.items():
        if old not in existing:
            skipped.append((old, "不存在"))
            continue
        if new in existing:
            skipped.append((old, f"目标名 {new} 已占用"))
            continue
        r = requests.patch(f"{HUB}/profiles/{requests.utils.quote(old)}",
                           json={"new_name": new}, timeout=15)
        d = r.json()
        if r.ok and d.get("ok"):
            alias[new] = old
            done.append((old, new))
            # 参考音缓存跟随改名（幂等：不存在就跳过）
            for ext in (".wav", ".txt"):
                src = os.path.join(REFS, f"interp_{old}{ext}")
                dst = os.path.join(REFS, f"interp_{new}{ext}")
                if os.path.exists(src) and not os.path.exists(dst):
                    os.rename(src, dst)
        else:
            skipped.append((old, str(d)[:80]))
    io.open(MAP_FILE, "w", encoding="utf-8").write(
        json.dumps(alias, ensure_ascii=False, indent=2))
    print("RENAMED:")
    for o, n in done:
        print(f"  {o} -> {n}")
    print("SKIPPED:")
    for o, why in skipped:
        print(f"  {o}: {why}")
    act = requests.get(f"{HUB}/profiles", timeout=10).json().get("active")
    print("active now:", act)


if __name__ == "__main__":
    main()
