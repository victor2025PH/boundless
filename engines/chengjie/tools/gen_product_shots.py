# -*- coding: utf-8 -*-
"""官网 / 目录站用「真实产品界面截图」生成器（Playwright；2026-08-28 实施78 P0-5）。

**为什么需要它**：G2 / Capterra / GetApp / Product Hunt 收录**强制要求 3-6 张真实
产品截图**，而 ``website/public/products/`` 里只有 ``prod-*.jpg`` 营销渲染图，一张真
工作台界面都没有——这是目录站收录的硬门槛（实施78 问题 A3），也是下载页「让我下载
442MB 未签名 exe，却不给我看软件长什么样」（U4）的根因。

**脱敏是本工具的主要设计约束**，不是附带功能。素材要发到公开目录站，泄露一个真实
客户名就是不可逆事故。所以采取三层：

1. **强制覆盖而非替换**：``page.route`` 拦下 chats / thread 的真实响应，把每一个
   身份与内容字段（name/username/phone/avatar/文本…）**无条件写成演示值**——
   whitelist 式，不做「找到真名再替换」的 blacklist（漏一条就泄露）。
   结构、状态位、徽章、SLA、自动化档位等全部保留真实 → UI 仍是真界面。
2. **截图前的可见文本自检**：先从 API 取真实数据建「禁止出现」集合（真实姓名 /
   用户名 / 手机号 / 消息片段），截图前 dump 页面**全部可见文本**做比对，
   命中任何一条 → 中止且**不落盘**。对齐仓库《媒体产物验证纪律》：
   「HTTP 200 + 文件有 KB 数」不构成内容验证，必须验内容。
3. **落盘后校验 PNG magic bytes**（同上纪律：产物必须验签名，不能只看尺寸）。

演示数据刻意选**东南亚跨境电商 + 多语言多平台**场景：既完成脱敏，又让截图正好展示
产品真实具备而中文真实数据展示不出来的卖点（真实入站 69% 是中文、id/th 为 0）。
这是业界标准做法（所有 SaaS 官网截图都是 demo data），UI 与能力均真实，未伪造功能。

用法::

    python tools/gen_product_shots.py                  # 生成到 website/public/products/screenshots
    python tools/gen_product_shots.py --headed          # 肉眼看抓取过程
    python tools/gen_product_shots.py --out D:/tmp/x    # 换输出目录
    python tools/gen_product_shots.py --self-proof      # 探测器自证：故意放行真名，PII 自检必须拦住

缺 playwright / 实例不可达 → SKIP exit 0（不污染门禁信号）；无 token → ABORT exit 2。
**副作用：无。** 全程只读 + route mock，不写实例任何数据。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parents[1]
DEFAULT_OUT = REPO_ROOT / "website" / "public" / "products" / "screenshots"
VIEWPORT = {"width": 1600, "height": 1000}

# ---------------------------------------------------------------------------
# 演示身份池：六渠道 × 多语言，覆盖官网主打的东南亚跨境场景。
# 顺序即分配顺序（按会话在列表中的位置取模），同一次运行内确定性可复现。
# 名字刻意用「常见但组合独特」的拼写，降低与真实客户重名的概率（重名会被
# 下方 build_forbidden() 检出并告警）。
# ---------------------------------------------------------------------------
DEMO_PEOPLE: List[Dict[str, str]] = [
    {"name": "Nurul Hidayanti", "username": "nurul.h", "lang": "id"},
    {"name": "Somchai Thanakit", "username": "somchai.t", "lang": "th"},
    {"name": "Nguyen Minh Tuan", "username": "minhtuan.ng", "lang": "vi"},
    {"name": "Grace Lim", "username": "gracelim", "lang": "en"},
    {"name": "Ahmad Fauzi", "username": "ahmad.fz", "lang": "id"},
    {"name": "Ratchada Suwan", "username": "ratchada.s", "lang": "th"},
    {"name": "Le Thi Mai", "username": "lethimai", "lang": "vi"},
    {"name": "Marcus Ong", "username": "marcusong", "lang": "en"},
    {"name": "Putri Ayu", "username": "putri.ayu", "lang": "id"},
    {"name": "Kittipong Sri", "username": "kittipong", "lang": "th"},
    {"name": "Tran Van Hung", "username": "tranvh", "lang": "vi"},
    {"name": "Chloe Wong", "username": "chloewong", "lang": "en"},
]

# 演示会话最后一条摘要（按语言取，展示「客户用母语问」）。
DEMO_LAST_MSG: Dict[str, List[str]] = {
    "id": [
        "Halo, apakah ukuran M masih ada stok?",
        "Berapa lama pengiriman ke Jakarta?",
        "Bisa kirim invoice-nya ke email saya?",
    ],
    "th": [
        "สวัสดีค่ะ ไซส์ M ยังมีของไหมคะ",
        "ส่งถึงกรุงเทพใช้เวลากี่วันคะ",
        "ขอใบเสร็จทางอีเมลได้ไหมคะ",
    ],
    "vi": [
        "Chào bạn, size M còn hàng không?",
        "Giao tới Hà Nội mất bao lâu?",
        "Cho mình xin hóa đơn qua email nhé",
    ],
    "en": [
        "Hi, is the size M still in stock?",
        "How long does shipping to Singapore take?",
        "Could you send the invoice to my email?",
    ],
}

# 演示消息流（幕 1/2 用）：入站带原文 + 译文，出站是坐席/AI 回复。
# 结构对齐 thread API 的 messages[]（direction / text / original_text /
# translated_text / language），前端双语气泡与翻译角标直接消费。
DEMO_THREAD: List[Dict[str, Any]] = [
    {
        "direction": "in",
        "original_text": "Halo, saya mau tanya. Pesanan saya sudah dikirim belum ya?",
        "translated_text": "Hello, I'd like to ask — has my order been shipped yet?",
        "language": "id",
    },
    {
        "direction": "out",
        "text": "Hi Nurul! Yes, your order shipped this morning. Tracking number is SPX-4471-2098 — it usually reaches Jakarta in 3-4 working days.",
        "language": "en",
    },
    {
        "direction": "in",
        "original_text": "Oke terima kasih. Kalau ukurannya tidak pas, bisa tukar?",
        "translated_text": "Okay, thank you. If the size doesn't fit, can I exchange it?",
        "language": "id",
    },
    {
        "direction": "out",
        "text": "Of course — free size exchange within 7 days of delivery. Just keep the original packaging and message me here, I'll arrange the pickup.",
        "language": "en",
    },
    {
        "direction": "in",
        "original_text": "Bagus sekali. Saya mau pesan 2 lagi untuk teman saya.",
        "translated_text": "That's great. I'd like to order 2 more for my friends.",
        "language": "id",
    },
]

# 演示 AI 草稿（幕 3 用）——刻意写成「像人写的」而非模板腔。
DEMO_DRAFT_TEXT = (
    "Thanks Nurul! I've reserved 2 more in size M for you. "
    "Shall I put all three in one parcel so you only pay shipping once?"
)


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。"""
    import yaml

    root = Path(data_root)
    for name in ("config.local.yaml", "config.yaml"):
        fp = root / "config" / name
        if not fp.exists():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


# ---------------------------------------------------------------------------
# PII 禁字集合：从真实 API 数据构建「截图里绝不允许出现」的字符串。
# ---------------------------------------------------------------------------
def _demo_value_set() -> set:
    """本工具会主动注入的演示值——它们不算泄露，必须从禁字集里排除。"""
    vals = set()
    for p in DEMO_PEOPLE:
        vals.add(p["name"])
        vals.add(p["username"])
        for part in p["name"].split():
            vals.add(part)
    for msgs in DEMO_LAST_MSG.values():
        vals.update(msgs)
    for m in DEMO_THREAD:
        for k in ("text", "original_text", "translated_text"):
            if m.get(k):
                vals.add(m[k])
    vals.add(DEMO_DRAFT_TEXT)
    return vals


# 通用词：真实客户的昵称/用户名有时恰好就是平台名或常用短词（实测某会话名就叫
# "LINE"）。这类词在 UI 里到处出现，收进禁字集会让自检变成**永真断言**——工具永远
# 拦住自己、一张也产不出。故在建集时剔除并打印，保持可观测（不静默丢）。
_GENERIC_TERMS = {
    "line", "telegram", "whatsapp", "messenger", "zalo", "instagram", "wechat",
    "facebook", "signal", "viber", "discord", "slack", "tiktok", "shopee", "lazada",
    "bot", "test", "admin", "user", "demo", "null", "none", "unknown", "default",
    "online", "offline", "persona", "account", "chat", "inbox",
    # 自家品牌词：实测有个受管账号名就叫「[无界科技] BOUNDELESS」，于是 "BOUNDLESS"
    # 进了禁字集。品牌名本就会出现在自家 UI 里，留着＝将来某次改版一加品牌标识
    # 就被永久误拦（且报错会指向「泄露」，极难查）。
    "boundless", "boundeless", "chatx", "lingox", "无界科技", "智聊", "通译",
}


def build_forbidden(chats: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """真实数据 → 禁字列表。返回 (禁字, 与演示值撞车的告警)。

    收录口径刻意保守（宁可少收也不能误报，误报会让工具永远无法产出）：
      - 姓名 / 用户名：长度 ≥ 3（"Li" 这种两字母词会命中正常英文文案）；
      - 手机号：长度 ≥ 6 的数字串（全收，手机号无误报风险）；
      - 消息文本：取长度 ≥ 12 的前 24 字符片段（短句如「好的」会命中 UI 文案）。
    与演示值完全相同的项从禁字集移除并告警——那说明某真实客户与演示人物重名，
    此时该项无法区分来源，应换演示名后重跑（本工具不静默吞掉这种情况）。
    """
    demo = _demo_value_set()
    forbidden: set = set()
    collisions: List[str] = []
    generic: List[str] = []

    def add(val: Any, min_len: int, clip: Optional[int] = None) -> None:
        s = str(val or "").strip()
        if len(s) < min_len:
            return
        if clip:
            s = s[:clip]
        if s.lower() in _GENERIC_TERMS:
            generic.append(s)
            return
        if s in demo:
            collisions.append(s)
            return
        forbidden.add(s)

    for c in chats:
        add(c.get("name"), 3)
        add(c.get("username"), 3)
        phone = re.sub(r"\D", "", str(c.get("phone") or ""))
        add(phone, 6)
        add(c.get("last_msg"), 12, 24)
        for m in c.get("messages") or []:
            for key in ("text", "original_text", "translated_text"):
                add(m.get(key), 12, 24)
    if generic:
        print(f"   [note] {len(set(generic))} 条真实值是通用词，已剔除以免自检永真：{', '.join(sorted(set(generic))[:8])}")
    return sorted(forbidden, key=len, reverse=True), sorted(set(collisions))


# ---------------------------------------------------------------------------
# 响应改写：强制覆盖身份/内容字段，保留其余真实结构
# ---------------------------------------------------------------------------
def _demo_for_index(idx: int) -> Dict[str, str]:
    return DEMO_PEOPLE[idx % len(DEMO_PEOPLE)]


def _scrub_message(m: Dict[str, Any], person: Dict[str, str], seq: int) -> Dict[str, Any]:
    """覆盖单条消息的全部文本与发件人字段（保留 ts/status/direction 等结构）。"""
    demo = DEMO_THREAD[seq % len(DEMO_THREAD)]
    direction = str(m.get("direction") or demo["direction"])
    if direction == "in":
        src = DEMO_THREAD[seq % len(DEMO_THREAD)]
        original = src.get("original_text") or src.get("text") or ""
        translated = src.get("translated_text") or ""
        m["original_text"] = original
        m["translated_text"] = translated
        m["text"] = original
        m["language"] = src.get("language") or person.get("lang") or "en"
        m["sender_name"] = person["name"]
    else:
        src = next(
            (d for d in DEMO_THREAD[seq % len(DEMO_THREAD):] if d["direction"] == "out"),
            DEMO_THREAD[1],
        )
        m["text"] = src.get("text") or ""
        m["original_text"] = ""
        m["translated_text"] = ""
        m["language"] = "en"
        m["sender_name"] = ""
    for key in ("reply_to_text", "reply_to_sender", "fail_reason"):
        if key in m:
            m[key] = ""
    m["sender_id"] = f"demo-{seq}"
    if m.get("media_ref"):
        m["media_ref"] = ""
        m["media_type"] = ""
    return m


# 演示账号标签：会话行「via account」徽章与账号 rail 都显示它。
# 真实值是我们自己的运营账号名 / 手机号（首跑漏出 "Wisley Gho"、"639270135480"）——
# 那不是客户隐私但同样绝不能进公开素材：暴露运营账号=招定向风控与骚扰。
DEMO_ACCOUNT_LABEL: Dict[str, str] = {
    "telegram": "Brand Official",
    "whatsapp": "Sales · SG",
    "messenger": "Store Page",
    "line": "Official Account",
    "zalo": "Zalo OA",
    "instagram": "Shop IG",
}


def _scrub_chat(c: Dict[str, Any], idx: int) -> Dict[str, Any]:
    """覆盖单个会话的身份与摘要（保留平台/状态/SLA/自动化档位等真实结构）。"""
    person = _demo_for_index(idx)
    lang = person.get("lang") or "en"
    pool = DEMO_LAST_MSG.get(lang) or DEMO_LAST_MSG["en"]
    c["name"] = person["name"]
    c["username"] = person["username"]
    c["phone"] = ""
    # account_id 是前端筛选/未读索引的键，改了会让账号视角与计数错位 → 只改显示名
    plat = str(c.get("platform") or "").lower()
    c["account_label"] = DEMO_ACCOUNT_LABEL.get(plat, "Brand Official")
    # 头像置空：前端回落首字母占位，避免真实客户头像进公开素材
    c["avatar_url"] = ""
    c["avatar_fp"] = ""
    c["last_msg"] = pool[idx % len(pool)]
    c["language"] = lang
    # 会话标签是坐席手打的自由文本（可能写着客户姓名/单号）→ 一律清空
    if isinstance(c.get("conv_tags"), list):
        c["conv_tags"] = []
    # 行级人设（`eff_persona` 等）→ 会话行的 `.conv-persona-badge`。这是内部配置
    # （中文人设名），既不该进公开素材、也会在英文画面里显成中文。按 key 名兜住
    # 全部变体，避免「今天叫 eff_persona、明天多一个 persona_display」时又漏。
    for key in list(c.keys()):
        if "persona" not in key.lower():
            continue
        val = c[key]
        if isinstance(val, str) and val:
            c[key] = DEMO_PERSONA_NAME
        elif isinstance(val, dict):
            _scrub_persona_payload(val)
    lm = c.get("last_message")
    if isinstance(lm, dict):
        _scrub_message(lm, person, idx)
    msgs = c.get("messages")
    if isinstance(msgs, list):
        for j, m in enumerate(msgs):
            if isinstance(m, dict):
                _scrub_message(m, person, j)
    return c


# 演示人设名：截图里「Persona: X」是要展示的卖点，但真实人设名属内部配置，
# 换成英文演示名既保住展示效果又不外泄（且更适合英文素材）。
DEMO_PERSONA_NAME = "Aria"


def _scrub_platform_status(ps: Any) -> None:
    """清洗 chats 响应里的 platform_status（**账号身份的真正来源**）。

    前端 ``rebuildAccountMeta`` 从这里抽 label/self_name 建 ``accountMeta``，
    再由 ``_acctLabel`` 喂给会话行的 via / 账号徽章——首跑漏出的 "Wisley Gho"
    走的就是这条路，而不是 ``chats[].account_label``（那条只是其中一个消费面）。
    key 形如 ``platform:account_id`` 且被前端当索引用，**只改值不改 key**。
    """
    if not isinstance(ps, dict):
        return
    for key, entry in ps.items():
        if not isinstance(entry, dict):
            continue
        plat = str(entry.get("platform") or str(key).split(":")[0] or "").lower()
        demo_label = DEMO_ACCOUNT_LABEL.get(plat, "Brand Official")
        entry["label"] = demo_label
        entry["self_name"] = demo_label
        if "self_username" in entry:
            entry["self_username"] = ""
        entry["self_avatar"] = ""
        if entry.get("persona_name"):
            entry["persona_name"] = DEMO_PERSONA_NAME


def _scrub_persona_payload(obj: Any) -> None:
    """递归把人设四层全景（effective/conv/account/legacy）的名字换成演示名。

    composer 的「Replying as X · 账号」取自 ``GET /api/persona/effective``，
    与 chats 是两条独立链——首跑实测中文人设名「苏娜」就是从这里漏进画面的
    （它不在 chats 的 PII 字段里，禁字集也不含它，所以自检不会拦：这条必须靠覆盖）。
    """
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k == "name" and isinstance(v, str) and v:
                obj[k] = DEMO_PERSONA_NAME
            elif k == "role" and isinstance(v, str) and v:
                obj[k] = "Sales rep"
            else:
                _scrub_persona_payload(v)
    elif isinstance(obj, list):
        for item in obj:
            _scrub_persona_payload(item)


def _install_routes(page: Any, *, leak_names: bool = False) -> None:
    """拦截收件箱数据接口：真响应打补丁（零生产写入）。

    ``leak_names=True`` 仅供 ``--self-proof``：故意放行真实姓名，用来证明
    PII 自检真的会拦（探测器不自证 = 门禁是摆设）。
    """

    def handle(route: Any) -> None:
        try:
            resp = route.fetch()
            body = resp.json()
        except Exception:  # noqa: BLE001
            route.continue_()
            return
        if leak_names:
            route.fulfill(response=resp)
            return
        try:
            if isinstance(body, dict):
                if isinstance(body.get("chats"), list):
                    for i, c in enumerate(body["chats"]):
                        if isinstance(c, dict):
                            _scrub_chat(c, i)
                if isinstance(body.get("chat"), dict):
                    _scrub_chat(body["chat"], 0)
                _scrub_platform_status(body.get("platform_status"))
                if isinstance(body.get("messages"), list):
                    person = _demo_for_index(0)
                    for j, m in enumerate(body["messages"]):
                        if isinstance(m, dict):
                            _scrub_message(m, person, j)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 响应改写异常，改为放弃该请求以免泄露：{e!r}")
            route.abort()
            return
        route.fulfill(
            status=resp.status,
            headers={"content-type": "application/json; charset=utf-8"},
            body=json.dumps(body, ensure_ascii=False),
        )

    def handle_drafts(route: Any) -> None:
        """注入一条演示待审草稿（幕 3）。

        与前两幕同一标准：**真实 UI + 演示内容**。AI 拟稿是产品的核心功能（不是
        为截图造的假界面），只是生产队列此刻恰好没有 pending 草稿——素材不该由
        「抓取那一刻队列里有没有货」决定。刻意**不带** chat_key/conversation_id：
        前端 `_loadDraftsBody` 对两者皆空的草稿放行（见其过滤分支），于是无需
        知道真实会话键，也就不会把真实标识写进画面。
        """
        try:
            resp = route.fetch()
        except Exception:  # noqa: BLE001
            route.continue_()
            return
        if leak_names:
            route.fulfill(response=resp)
            return
        import time as _t
        demo = {
            "ok": True,
            "drafts": [
                {
                    "draft_id": "demo-draft-1",
                    "autopilot_level": "L2",
                    "risk_level": "low",
                    "created_ts": _t.time() - 420,  # 7 分钟前 → 稿龄 chip 显示 "7m"
                    "approve_blocked": "",
                    "peer_name": DEMO_PEOPLE[0]["name"],
                    "peer_last_text": DEMO_THREAD[4]["original_text"],
                    "draft_text": DEMO_DRAFT_TEXT,
                }
            ],
        }
        route.fulfill(
            status=200,
            headers={"content-type": "application/json; charset=utf-8"},
            body=json.dumps(demo, ensure_ascii=False),
        )

    def handle_persona(route: Any) -> None:
        try:
            resp = route.fetch()
            body = resp.json()
        except Exception:  # noqa: BLE001
            route.continue_()
            return
        if leak_names:
            route.fulfill(response=resp)
            return
        _scrub_persona_payload(body)
        route.fulfill(
            status=resp.status,
            headers={"content-type": "application/json; charset=utf-8"},
            body=json.dumps(body, ensure_ascii=False),
        )

    page.route("**/api/unified-inbox/chats*", handle)
    page.route("**/api/unified-inbox/thread*", handle)
    page.route("**/api/persona/effective*", handle_persona)
    # ⚠ 用正则而非 glob：playwright 的 URL glob 里 `?` 是**单字符通配符**，
    # `**/api/drafts?*` 会去匹配 `/api/draftsX…` 而永远错过真正的查询串请求
    # （实测表现为 mock 完全不生效、面板一直空）。同时避免误拦
    # `/api/drafts/stats` 与 `/api/drafts/autosend-status`（那两个是别的语义）。
    page.route(re.compile(r"/api/drafts\?"), handle_drafts)
    # 真实客户头像：一律拦掉（置空 avatar_url 后前端不该再请求，双保险）
    page.route(re.compile(r"/static/(avatars|persona_albums)/"), lambda r: r.abort())


# ---------------------------------------------------------------------------
# 可见文本自检
# ---------------------------------------------------------------------------
# 单一扫描口径：命中判定与命中定位共用这一段 JS。
#
# ⚠ 扫描范围必须与截图范围一致，否则两头都错：
#   - 只扫 body.innerText → 把**视口外**（滚动区/隐藏抽屉里）的账号卡也算泄露 →
#     永远拦住，工具产不出任何素材（首版实测就是这样，`.acct-card` 在抽屉里）；
#   - 不做可见性判断 → display:none 的模板残留也算泄露，同样永假。
# 截图走视口（非 full_page），所以这里按「可见 且 与视口相交」过滤，
# 并**保守地把部分露出**也算在内（只要有一个像素进画面就算）。
_SCAN_JS = """(needles) => {
  const out = [], seen = new Set();
  const vw = window.innerWidth, vh = window.innerHeight;
  const intersectsViewport = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    return r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
  };
  for (const el of document.querySelectorAll('*')) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    if (!intersectsViewport(el)) continue;
    // 只看自有文本节点，避免每个祖先都重复上报同一条
    let own = '';
    for (const n of el.childNodes) if (n.nodeType === 3) own += n.nodeValue;
    let attrs = '';
    for (const a of ['title', 'alt', 'placeholder', 'value']) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v) attrs += ' ' + v;
    }
    const hay = own + attrs;
    if (!hay.trim()) continue;
    for (const nd of needles) {
      if (!nd || !hay.includes(nd)) continue;
      const cls = (typeof el.className === 'string' ? el.className : '');
      const key = nd + '|' + el.tagName + '|' + cls;
      if (seen.has(key)) continue;
      seen.add(key);
      const parts = [];
      let e = el;
      for (let i = 0; i < 4 && e && e.tagName; i++) {
        const c = (typeof e.className === 'string' && e.className) ? '.' + e.className.split(/\\s+/)[0] : '';
        parts.unshift(e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + c);
        e = e.parentElement;
      }
      out.push({needle: nd, path: parts.join(' > '), ctx: hay.trim().slice(0, 70)});
    }
  }
  return out;
}"""


# 英文素材质量检查：英文 UI 下画面里**不该出现中文**。
# 这不是脱敏（中文残留不泄露隐私），而是素材专业度——实施78 的 M9/A2 正是在批评
# 「英文页混中文」暗示产品是半成品汉化。它同时是一份产品 i18n 债清单：
# 首跑实测右栏副驾把关系阶段渲染成「战绩/升温」（缺英文映射）。
# 刻意只报告不遮挡：遮住等于把债藏起来，而这份清单本身就是交付物。
_CJK_SCAN_JS = """() => {
  const out = [], seen = new Set();
  const vw = window.innerWidth, vh = window.innerHeight;
  const cjk = /[\\u4e00-\\u9fff\\u3040-\\u30ff]/;
  for (const el of document.querySelectorAll('*')) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (!(r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw)) continue;
    let own = '';
    for (const n of el.childNodes) if (n.nodeType === 3) own += n.nodeValue;
    own = own.trim();
    if (!own || !cjk.test(own)) continue;
    const parts = [];
    let e = el;
    for (let i = 0; i < 3 && e && e.tagName; i++) {
      const c = (typeof e.className === 'string' && e.className) ? '.' + e.className.split(/\\s+/)[0] : '';
      parts.unshift(e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + c);
      e = e.parentElement;
    }
    const path = parts.join(' > ');
    const key = path + '|' + own.slice(0, 20);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({path: path, text: own.slice(0, 40)});
  }
  return out;
}"""


def scan_cjk(page: Any) -> List[Dict[str, str]]:
    try:
        return page.evaluate(_CJK_SCAN_JS) or []
    except Exception:  # noqa: BLE001
        return []


def scan_pii(page: Any, forbidden: List[str]) -> List[Dict[str, str]]:
    """返回命中点（含 DOM 路径与上下文）；空列表 = 干净。

    命中定位不是调试便利，是必需品：拦下时必须能回答「这条真实数据从哪漏的」。
    首跑实测漏点全在最初枚举的 PII 字段清单之外——账号 rail 的 account_label、
    WhatsApp 的 chat_key（它本身就是手机号）、顶栏 SLA pill 里的逾期会话名。
    """
    try:
        return page.evaluate(_SCAN_JS, forbidden) or []
    except Exception as e:  # noqa: BLE001
        # 扫不动时**按最坏情况处理**：报一条合成命中，宁可拦住也不许在无保护下落盘
        return [{"needle": "(scan-failed)", "path": "-", "ctx": f"PII 扫描异常：{e!r}"}]


def _png_ok(fp: Path) -> bool:
    """magic bytes 验证（媒体产物验证纪律：不能只看文件尺寸）。"""
    try:
        with fp.open("rb") as fh:
            return fh.read(8) == b"\x89PNG\r\n\x1a\n"
    except Exception:  # noqa: BLE001
        return False


# bot 判定不看徽章：徽章被上面的 CSS 隐藏，且它的 display 在切会话后是**粘滞**的
# （实测切到非 bot 会话后仍为可见），拿它当判据会把所有候选都误杀。改看会话行上的
# bot 标记（行渲染自 chats 数据，切换即更新）。
_CHAT_STATE_JS = """() => {
  const area = document.getElementById('msg-area');
  const rows = area ? area.querySelectorAll('.msg-row').length : 0;
  const xl = area ? area.querySelectorAll('.msg-translation').length : 0;
  // 右栏工具箱 tab 是否可用（幕 4 用它，不看已退役的 composer 语音按钮）
  const voice = !!document.querySelector('.ws-cp-tab[data-tab="tools"]');
  const draft = (() => {
    const d = document.getElementById('draft-panel');
    return !!(d && getComputedStyle(d).display !== 'none' && (d.innerText || '').trim().length > 20);
  })();
  return {rows, xl, voice, draft};
}"""


def _open_showcase_chat(page: Any, *, probe: int = 6) -> bool:
    """打开一个适合当展示素材的会话；返回是否找到。

    评分偏好「有翻译行」（幕 2 主题）与「消息多」（画面不空）；语音入口 / 待审草稿
    可用时加分，因为后面几幕直接复用同一个会话。先整轮探查取最佳，再回到它——
    翻译行是异步渲染的，「见到第一个凑合的就停」会经常拿到还没渲染完的画面。
    """
    # ⚠ 用 data-key 而非 nth 索引定位：会话列表被 realtime 轮询按最后消息时间**实时重排**，
    # 探查过程中 nth(i) 指向的行会漂移甚至 detach（实测 click 直接抛异常）。
    keys: List[str] = page.evaluate(
        """() => Array.from(document.querySelectorAll('#conv-items .conv-item'))
                   .map(r => r.getAttribute('data-key') || '').filter(Boolean)"""
    ) or []
    if not keys:
        return False

    def click_key(key: str) -> bool:
        """用 JS 按 data-key 找元素再 .click()。

        刻意不走 ``page.locator('[data-key="..."]')``：chat_key 里带 ``:``、``+``、
        ``@`` 等字符，拼进 CSS 属性选择器要自己做转义，错一个字符就静默匹配不到
        （实测表现为「点了但消息永远不出现」，比报错更难查）。传参给 evaluate
        没有任何转义问题。
        """
        try:
            return bool(page.evaluate(
                """(key) => {
                  const el = Array.from(document.querySelectorAll('#conv-items .conv-item'))
                    .find(r => r.getAttribute('data-key') === key);
                  if (!el) return false;
                  el.scrollIntoView({block: 'center'});
                  el.click();
                  return true;
                }""",
                key,
            ))
        except Exception:  # noqa: BLE001
            return False

    print(f"   [probe] 会话行 {len(keys)} 个，探查前 {min(len(keys), probe)} 个")
    best_key, best_score = "", -1
    for key in keys[:probe]:
        if not click_key(key):
            print("   [probe] click miss")
            continue
        # 等消息真的渲染出来再评分：固定 sleep 会在慢会话上量到空画面
        # （实测 2s 时全部量成 0 条 → 每个候选都被判「消息太少」而全军覆没）
        try:
            page.wait_for_selector("#msg-area .msg-row", timeout=7000)
        except Exception:  # noqa: BLE001
            print("   [probe] no .msg-row within 7s")
            continue
        page.wait_for_timeout(1500)  # 让翻译行/角标补渲染
        st = page.evaluate(_CHAT_STATE_JS) or {}
        n_rows = int(st.get("rows") or 0)
        print(f"   [probe] rows={n_rows} xl={st.get('xl')} voice={st.get('voice')} draft={st.get('draft')}")
        if n_rows < 3:
            continue
        score = (n_rows + 500 * int(st.get("xl") or 0)
                 + 300 * bool(st.get("voice")) + 400 * bool(st.get("draft")))
        if score > best_score:
            best_score, best_key = score, key
    if not best_key:
        return False
    if not click_key(best_key):
        return False
    try:
        page.wait_for_selector("#msg-area .msg-row", timeout=7000)
    except Exception:  # noqa: BLE001
        return False
    page.wait_for_timeout(2500)
    return True


class Shooter:
    def __init__(self, out_dir: Path, forbidden: List[str], *, diagnose: bool = True) -> None:
        self.out = out_dir
        self.forbidden = forbidden
        self.diagnose = diagnose
        self.saved: List[str] = []
        self.failed: List[str] = []
        self.cjk: Dict[str, List[Dict[str, str]]] = {}

    def shoot(self, page: Any, name: str, *, selector: Optional[str] = None) -> bool:
        spots = scan_pii(page, self.forbidden)
        if spots:
            needles = sorted({s["needle"] for s in spots})
            preview = ", ".join(n[:18] + "…" for n in needles[:4])
            print(f"[BLOCK] {name}: 画面内命中 {len(needles)} 条真实数据（{preview}）—— 拒绝落盘")
            if self.diagnose:
                for spot in spots[:14]:
                    print(f"        ↳ {spot['path']}\n            «{spot['ctx']}»")
            self.failed.append(name)
            return False
        self.out.mkdir(parents=True, exist_ok=True)
        fp = self.out / f"{name}.png"
        target = page.locator(selector) if selector else page
        target.screenshot(path=str(fp))
        if not _png_ok(fp):
            print(f"[FAIL] {name}: 落盘文件不是有效 PNG（magic bytes 不符）")
            fp.unlink(missing_ok=True)
            self.failed.append(name)
            return False
        kb = fp.stat().st_size // 1024
        print(f"[OK]   {name}.png  ({kb} KB)  PII 自检通过（{len(self.forbidden)} 条禁字零命中）")
        self.saved.append(name)
        cjk = scan_cjk(page)
        if cjk:
            self.cjk[name] = cjk
            print(f"       ⚠ 画面内有 {len(cjk)} 处中文残留（英文素材质量债，见末尾清单）")
        return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def fetch_real_chats(base: str, token: str) -> List[Dict[str, Any]]:
    """只读拉真实会话，用于建 PII 禁字集（不落盘、不打印内容）。"""
    import http.cookiejar
    import urllib.parse
    import urllib.request

    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(base + "/login", urllib.parse.urlencode({"auth_token": token}).encode(), timeout=15)
    raw = op.open(base + "/api/unified-inbox/chats?limit=40", timeout=30).read().decode("utf-8")
    return (json.loads(raw) or {}).get("chats") or []


def run(base: str, token: str, out_dir: Path, *, headed: bool = False,
        self_proof: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    print("== 拉真实数据建 PII 禁字集（只读）==")
    try:
        chats = fetch_real_chats(base, token)
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达：{e!r}")
        return 0
    forbidden, collisions = build_forbidden(chats)
    print(f"   真实会话 {len(chats)} 个 → 禁字 {len(forbidden)} 条")
    if collisions:
        print(f"[WARN] {len(collisions)} 条真实值与演示值重名，已从禁字集移除（建议换演示名后重跑）：")
        for c in collisions[:6]:
            print(f"        - {c}")
    if not forbidden:
        print("[ABORT] 禁字集为空 —— 自检会退化成永真断言，拒绝在无保护的情况下产出素材")
        return 2

    # 自证模式预期就是全拦，逐条诊断只会刷屏真实数据 → 只在正式模式开诊断
    shooter = Shooter(out_dir, forbidden, diagnose=not self_proof)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        # 新手引导（coach marks）是 localStorage 驱动的，首访必弹并盖住右栏——
        # （cp-tour 已于 2026-08-31 随新手引导退役，旧的「预置已看过」步骤删除。）
        try:
            ctx.request.post(base + "/login", form={"auth_token": token})
            page = ctx.new_page()
            _install_routes(page, leak_names=self_proof)
            page.goto(base + "/workspace?lang=en", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => typeof window.setPlatFilter === 'function'", timeout=25000)
            page.wait_for_timeout(4000)

            # 清场：① 挡画面的引导/弹层；② **含运营账号身份且对产品截图非必需**的组件——
            # 账号 rail（`#acct-dock`，逐条列出我们自己的账号名）与顶栏 SLA pill
            # （`#ws-sla`，把逾期客户的昵称显示在最显眼处）。隐藏它们不影响
            # 「这是真界面」——会话列表 / 消息流 / AI 拟稿 / 语音才是要展示的主体。
            #
            # ⚠ 必须用注入 CSS 而不是 element.style：前端在 loadChats / 打开会话时会
            # 重建这些 DOM，inline style 当场失效（实测幕 2 里账号 rail 又冒了出来）。
            # `#hdr-bot-badge` 同属此列：它是 peer_bot_guard 的**内部风控判定**（不是对外
            # 功能），而它的 title 里带对端账号标识（实测漏出 "tgzkw_bot"）。会话内容
            # 既已整体替换为演示对话，这个残留的真实上下文标记就该一并去掉。
            page.add_style_tag(
                content=(
                    ".onboard-modal, .guided-tour, #assistant-ball, .ws-degrade-bar,"
                    " .stale-banner, #acct-dock, #ws-sla, #hdr-bot-badge,"
                    # 瞬时 toast：内容是运营通知（且实测把关系阶段渲染成中文
                    # 「初识 → 试探/升温」），碰巧被截进画面纯属噪声
                    " #ws-toast-box"
                    " { display: none !important; }"
                )
            )
            page.evaluate("() => document.body.classList.remove('adk-on')")

            # 幕 1：统一收件箱总览（多平台会话列表）
            shooter.shoot(page, "01-unified-inbox")

            # 幕 2：打开一个**合适的**会话看消息流（多语言翻译双语气泡）。
            # 选会话不能取第一个就用：实测首个会话是 bot 会话（`tgzkw_bot`），
            # 既会把真实 bot 账号名漏进 bot 徽章的 title，也不是要展示的东西
            # ——产品截图要展示的是**客户对话**。故按「非 bot + 有足够消息」筛，
            # 并优先挑已渲染出翻译行的会话（幕 2 的主题就是翻译）。
            opened = _open_showcase_chat(page)
            if opened:
                shooter.shoot(page, "02-multilingual-translation")
            else:
                print("[SKIP] 没找到合适的展示会话（非 bot + 有消息），跳过幕 2/3/4")

            # 幕 3：AI 拟稿草稿卡。面板是坐席主动展开的（`aiDraftOn` 开关 + `.show`），
            # 默认收起 → 必须先点开，否则永远量不到（首版就卡在这）。
            try:
                page.evaluate(
                    """() => {
                      if (typeof _openAiDraftPanel === 'function') { _openAiDraftPanel(); return; }
                      if (typeof toggleAiDraft === 'function') { toggleAiDraft(); }
                    }"""
                )
                page.wait_for_timeout(2500)
            except Exception as e:  # noqa: BLE001
                print(f"[SKIP] 展开 AI 草稿面板失败：{e!r}")
            has_draft = page.evaluate(
                """() => {
                  const p = document.getElementById('draft-panel');
                  if (!p) return false;
                  const st = getComputedStyle(p);
                  return st.display !== 'none' && (p.innerText || '').trim().length > 20;
                }"""
            )
            if has_draft:
                shooter.shoot(page, "03-ai-draft-review")
            else:
                print("[SKIP] 待审草稿面板未出现 → 幕 3 跳过")

            # 幕 4：右栏工具箱（语音克隆 / 发图 / 培育等出货副驾组件）。
            # 刻意**不点** composer 的 `#voice-cfg-btn`：那个入口已退役，模板里
            # `display:none` 是刻意保留的「尸体」（只为让测试与孤儿引用门禁能找到元素），
            # 真正在用的语音能力是右栏 cp-voice 组件。
            try:
                tab = page.locator('.ws-cp-tab[data-tab="tools"]')
                if tab.count() > 0:
                    tab.first.click()
                    page.wait_for_timeout(1200)
                    # 点完鼠标停在 tab 上会挂出「工具卡可以浮出成小窗」的 hover 提示，
                    # 正好盖住翻译工具组 → 移到中性位置等它消散再拍
                    page.mouse.move(660, 940)
                    page.wait_for_timeout(2400)
                    shooter.shoot(page, "04-voice-and-toolbox")
                else:
                    print("[SKIP] 右栏工具箱 tab 不存在 → 幕 4 跳过")
            except Exception as e:  # noqa: BLE001
                print(f"[SKIP] 幕 4 工具箱：{e!r}")
        finally:
            browser.close()

    print()
    print(f"== 落盘 {len(shooter.saved)} 张 → {out_dir} ==")
    for n in shooter.saved:
        print(f"   {n}.png")
    if shooter.cjk:
        total = sum(len(v) for v in shooter.cjk.values())
        print()
        print(f"== 英文 UI 里的中文残留 {total} 处（产品 i18n 债，非隐私问题）==")
        for name, spots in shooter.cjk.items():
            print(f"   [{name}]")
            for s in spots[:10]:
                print(f"      {s['path']}\n         «{s['text']}»")
    if self_proof:
        # 自证模式：放行真名后自检必须拦住全部，否则说明自检形同虚设
        if shooter.saved:
            print(f"[SELF-PROOF FAIL] 放行真实姓名后仍产出 {len(shooter.saved)} 张 —— PII 自检没有生效")
            return 1
        print("[SELF-PROOF PASS] 放行真实姓名时全部被拦，PII 自检有效")
        return 0
    if not shooter.saved:
        print("[FAIL] 一张都没产出")
        return 1
    if shooter.failed:
        print(f"[WARN] {len(shooter.failed)} 幕被拦或失败：{', '.join(shooter.failed)}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="生成官网/目录站用真实产品界面截图（只读 + route mock 脱敏，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"实例地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help="实例数据根（读 auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"输出目录（默认 {DEFAULT_OUT}）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--self-proof", action="store_true",
                    help="探测器自证：故意放行真名，PII 自检必须全部拦住")
    args = ap.parse_args(argv)

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] 未安装 playwright（pip install playwright && playwright install chromium）")
        return 0

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2

    print(f"== 目标 {args.base}（视口 {VIEWPORT['width']}x{VIEWPORT['height']} @2x）==")
    return run(args.base, token, Path(args.out), headed=args.headed, self_proof=args.self_proof)


if __name__ == "__main__":
    sys.exit(main())
