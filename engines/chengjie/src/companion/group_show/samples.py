"""剧本台词样例 —— 「这本戏到底会说出什么话」的可见化（纯函数 + 一层薄 I/O）。

市场侧看剧本库只能看到 ``intent``（意图），而意图是写给导演看的、不是客户会读到的话。
「软广 4 级」到底长什么样，此前只有两条路：勾「用真 LLM」等几分钟，或者上线之后到群里
看——两条都不适合选戏的当口。

**为什么不另建一套预渲染管线**
------------------------------
真 LLM 排练本来就在产出真实台词（和真发同一条生成路径）。与其新起一套「夜间批量生成」
基建（要 API key、要计划任务、要自愈，还得防它和真发链漂），不如把**已经发生过的那次
排练**的台词存下来：样例天然与真发同源，谁排练一次谁就顺手给全团队备了货。

**指纹即失效**
--------------
样例按剧本内容指纹（:func:`playbook_fingerprint`）登记。剧本被改过一个字——换了意图、
调了软广、加了一拍——指纹就变，旧样例立刻判**陈旧**并停止展示。这条是硬要求：展示与
当前剧本对不上的台词，比不展示坏得多，因为它看起来完全正常，而运营会照着它做投放判断
（语音预渲染的参考音指纹踩过同一个坑：换了声还在发旧音色）。

**落盘只写实例数据区，绝不写仓库**
----------------------------------
``config/playbooks/`` 在很多部署里是**共享只读代码根**且被 git 跟踪。样例是运行期产物，
写进去会把仓库改脏（可能撞重启脚本的脏树闸门），打包态更是根本存不下。故调用方必须传
实例侧的可写路径；解析不出可写位置就**不存**（少一份样例远好过污染代码根）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: 每本戏展示几条样例。2 条足够看出「开场自不自然」+「种草那句露不露骨」，
#: 再多就变成把整场戏抄一遍，选戏时没人读得完。
SAMPLE_LIMIT = 2

#: 单条样例最长保留多少字（真发台词本就要求「像群友插话一两句」，超长本身即异常）。
SAMPLE_MAX_CHARS = 300

#: 样例库最多留多少本戏，防止改名/试验型剧本把文件撑大。
MAX_ENTRIES = 50

#: 样例文件名（落在实例可写配置区，不在仓库 playbooks 目录里）。
SAMPLES_FILENAME = "playbook_samples.json"


def playbook_fingerprint(pb: Any) -> str:
    """剧本内容指纹：**只把影响台词的字段**算进去。

    含 ``soft_ad_level`` 与每拍的 (id, role, intent, product, soft, pace)——它们任何
    一个变了，生成出来的话就会变，旧样例即失真。刻意**不含** ``name``：改个显示名
    不影响台词，却会白白作废一批还准确的样例。
    """
    parts: List[str] = [
        str(getattr(pb, "id", "") or ""),
        str(int(getattr(pb, "soft_ad_level", 0) or 0)),
    ]
    for b in (getattr(pb, "beats", ()) or ()):
        parts.append("|".join([
            str(getattr(b, "id", "") or ""),
            str(getattr(b, "role", "") or ""),
            str(getattr(b, "intent", "") or ""),
            str(getattr(b, "product", "") or ""),
            str(int(getattr(b, "soft", -1))),
            str(getattr(b, "pace", "") or ""),
        ]))
    blob = "\n".join(parts).encode("utf-8", "replace")
    return hashlib.sha1(blob).hexdigest()[:16]


def pick_sample_lines(lines: Sequence[Any],
                      limit: int = SAMPLE_LIMIT) -> List[Dict[str, Any]]:
    """从一场排练里挑最有代表性的几句。

    挑法不是「取前 N 条」：市场要判断的是**这本戏露不露骨**，而露骨与否全在软广最高
    的那一拍（种草时刻）。所以取「软广最高的一句」+「开场第一句」（开场决定像不像
    真人闲聊），按原顺序展示。

    真人插话（``kind == 'human'``）一律剔除：那是运营自己敲进去的测试话，不是本剧本
    会产出的文案，混进样例等于拿自己的话冒充 AI 的话。
    """
    ours = [ln for ln in (lines or ())
            if str(getattr(ln, "kind", "") or "") != "human"
            and str(getattr(ln, "text", "") or "").strip()]
    if not ours:
        return []
    n = max(1, int(limit or SAMPLE_LIMIT))
    chosen: List[Any] = [max(ours, key=lambda ln: int(getattr(ln, "soft_level", 0) or 0))]
    for ln in ours:                      # 开场（按 seq 最小）补进来
        if ln not in chosen:
            chosen.append(ln)
            break
    chosen = chosen[:n]
    chosen.sort(key=lambda ln: int(getattr(ln, "seq", 0) or 0))
    out: List[Dict[str, Any]] = []
    for ln in chosen:
        text = str(getattr(ln, "text", "") or "").strip()
        if len(text) > SAMPLE_MAX_CHARS:
            text = text[:SAMPLE_MAX_CHARS] + "…"
        out.append({
            "beat_id": str(getattr(ln, "beat_id", "") or ""),
            "role": str(getattr(ln, "role", "") or ""),
            "soft": int(getattr(ln, "soft_level", 0) or 0),
            "text": text,
        })
    return out


def build_sample_entry(pb: Any, lines: Sequence[Any], *,
                       now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """(剧本, 排练台词) → 一条样例登记；挑不出台词则 ``None``（不写空壳）。"""
    picked = pick_sample_lines(lines)
    if not picked:
        return None
    return {
        "fp": playbook_fingerprint(pb),
        "ts": float(now if now is not None else time.time()),
        "lines": picked,
    }


def is_stale(entry: Any, fp: str) -> bool:
    """样例是否已与当前剧本对不上（缺登记也算陈旧 → 不展示）。"""
    if not isinstance(entry, dict):
        return True
    return str(entry.get("fp") or "") != str(fp or "")


def samples_path(config_dir: Any) -> Optional[Path]:
    """样例文件落点：**实例可写配置区**。拿不到 → ``None``（宁可不存也不写仓库）。"""
    if not config_dir:
        return None
    try:
        return Path(config_dir) / SAMPLES_FILENAME
    except (TypeError, ValueError):
        return None


def load_samples(path: Any) -> Dict[str, Any]:
    """读样例库；文件缺失/损坏一律空表（样例是锦上添花，绝不阻断选戏）。"""
    p = samples_path(path) if not str(path or "").endswith(SAMPLES_FILENAME) \
        else Path(path)
    if p is None or not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 半写入/手改坏了都按「没有样例」处理
        logger.debug("[group_show.samples] 样例库读取失败（按空处理）", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def save_sample(path: Any, playbook_id: str, entry: Dict[str, Any], *,
                max_entries: int = MAX_ENTRIES) -> bool:
    """写入/覆盖一本戏的样例。写失败返回 ``False``——调用方**不得**因此让排练报错。"""
    p = samples_path(path) if not str(path or "").endswith(SAMPLES_FILENAME) \
        else Path(path)
    pid = str(playbook_id or "").strip()
    if p is None or not pid or not isinstance(entry, dict):
        return False
    data = load_samples(p)
    data[pid] = entry
    if len(data) > max_entries:      # 超量先丢最旧的（按登记时刻）
        keep = sorted(data.items(), key=lambda kv: float(
            (kv[1] or {}).get("ts") or 0.0), reverse=True)[:max_entries]
        data = dict(keep)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001 —— 只读安装目录/磁盘满都不该把排练搞挂
        logger.debug("[group_show.samples] 样例库写入失败（已忽略）", exc_info=True)
        return False


def samples_for(path: Any, pb: Any) -> Dict[str, Any]:
    """取某本戏**当前有效**的样例（陈旧即视为没有）。

    返回 ``{"lines": [...], "ts": float, "stale": bool}``：``stale=True`` 时 lines
    为空但仍如实告诉前端「有过样例、但剧本改了」——那和「从没排练过」该说的话不一样
    （前者提示重排一次即可，后者要先教会用户去勾真 LLM）。
    """
    fp = playbook_fingerprint(pb)
    entry = (load_samples(path) or {}).get(str(getattr(pb, "id", "") or ""))
    if not isinstance(entry, dict):
        return {"lines": [], "ts": 0.0, "stale": False}
    if is_stale(entry, fp):
        return {"lines": [], "ts": float(entry.get("ts") or 0.0), "stale": True}
    lines = entry.get("lines")
    return {
        "lines": list(lines) if isinstance(lines, list) else [],
        "ts": float(entry.get("ts") or 0.0),
        "stale": False,
    }
