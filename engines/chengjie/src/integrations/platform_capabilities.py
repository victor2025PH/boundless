"""平台能力矩阵 —— 从**代码结构**推导「哪个平台能做什么」，而不是靠人写文档。

**为什么要有这个模块**：2026-07-31 有人问「坐席全自动聊天在 telegram/whatsapp/
line/messenger 上的逻辑和能力是不是一样」，答这个问题要通读四个 worker 类；而当时
代码里至少有三处注释是**错的或已过期**（`mark_read` 的 docstring 写着「WA 暂无」，
而 WhatsApp worker 早就实现了；`send_chat_action` 写着「LINE 暂无」，没说清那是
协议层不可做还是我们没接）。人写的能力清单必然漂移，所以这里改成从代码推。

**判据只取「可靠可判定」的那部分**：worker 上有没有那个方法。这与编排器**运行时
真正用的判据完全同源**——`AccountOrchestrator.owns_media()` 就是
`hasattr(worker, "send_media")`，`mark_read`/`send_chat_action` 也都是
`hasattr` 后再调。所以本矩阵不是「另一套说法」，它读的就是那套说法本身。

**刻意不覆盖**「引用回复 / 撤回 / 贴纸」这类：它们无法由结构可靠判定——形参叫
`reply_to` 不代表真的透传（LINE 的 `send()` 收了这个参数却忽略它，一直忽略到
2026-07-31 才接上；Messenger 至今仍是收了不用）。按签名推断会**造出假阳性**，
那正是本模块要消灭的东西。宁可少答，不可错答。

用法::

    from src.integrations.platform_capabilities import capability_matrix
    capability_matrix(config)   # -> {"line:protocol": {"send_text": True, ...}, ...}

渲染成文档见 ``scripts/platform_matrix.py``（产物 ``docs/平台能力矩阵.md``，
由 ``tests/test_platform_matrix.py`` 钉住与代码一致）。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 能力 → worker 上对应的方法名。**必须与编排器的实际判据一致**：
#: owns_media→send_media / mark_read→mark_read / send_chat_action→typing。
CAPABILITY_METHODS: Dict[str, str] = {
    "send_text": "send",
    "send_media": "send_media",
    "mark_read": "mark_read",
    "typing": "send_chat_action",
}

CAPABILITY_LABELS: Dict[str, str] = {
    "send_text": "发文本",
    "send_media": "发图/语音/视频",
    "mark_read": "已读回执",
    "typing": "正在输入",
}

#: 参与矩阵的 worker（与 ``ensure_builtin_workers`` 注册的那批对齐；
#: 漏登记会被 ``tests/test_platform_matrix.py`` 的注册表比对门禁点名）。
#: 之所以在这里显式列而不是只枚举注册表：注册是**按配置与依赖门控**的，
#: 缺 pyrogram / okline 的机器上注册表会少几项，那样生成的文档就成了机器相关的。
WORKERS: List[Tuple[str, str, str, str]] = [
    # (platform, mode, 模块, 类名)
    ("telegram", "protocol", "src.integrations.account_orchestrator",
     "TelegramProtocolWorker"),
    ("telegram", "protocol(companion)",
     "src.integrations.telegram_companion_worker", "TelegramCompanionWorker"),
    ("whatsapp", "protocol", "src.integrations.account_orchestrator",
     "WhatsAppProtocolWorker"),
    ("messenger", "web", "src.integrations.account_orchestrator",
     "MessengerWebWorker"),
    ("line", "protocol", "src.integrations.account_orchestrator",
     "LineProtocolWorker"),
    # 2026-08-19 群/频道 P0：Zalo/Instagram 个人号 web worker 此前不在矩阵——
    # 「未进表」让这两个平台的能力问题只能翻源码，正是本表要消灭的成本。
    ("zalo", "web", "src.integrations.account_orchestrator",
     "ZaloPersonalWorker"),
    ("instagram", "web", "src.integrations.account_orchestrator",
     "InstagramWebWorker"),
]

#: 入站接线点：矩阵行 key → 站点。两种形态：
#:   ("<py 模块>", "<函数路径；. 分隔嵌套>")  → AST 判定（Python handler）
#:   ("js", "<引擎根相对路径>")               → 文本判定（Node 边车；payload 在 JS 里
#:                                              构造，Python AST 够不着）
#: ⚠ 此前 WA/Messenger 挂在共享 ingest 路由 ``api_protocol_ingest`` 上——那只能证明
#: 「路由会转发」，不能证明「边车真的送了」（Zalo 边车入站媒体只发 [媒体] 占位、
#: 根本不带 media_type；IG 边车更是显式 chat_type:""）。判定点必须下沉到事实源。
INBOUND_SITES: Dict[str, Tuple[str, str]] = {
    "telegram:protocol": ("src.integrations.account_orchestrator",
                          "TelegramProtocolWorker._wire_inbound._on_msg"),
    # companion 运行时走 A 线 client 自己的入站（生产实际在跑的就是这档）
    "telegram:protocol(companion)": ("src.client.telegram_client",
                                     "TelegramClient._process_message_async"),
    # impl85 阶段1（工单#44）把 payload 构造从 _start_receiver._on_msg 闭包提为
    # _ingest_inbound 方法（SSE 与拉取兜底共用）——登记随代码走，钉旧闭包只会判 False。
    "line:protocol": ("src.integrations.account_orchestrator",
                      "LineProtocolWorker._ingest_inbound"),
    "whatsapp:protocol": ("js", "services/whatsapp-baileys/server.js"),
    "messenger:web": ("js", "services/messenger-web/server.js"),
    "zalo:web": ("js", "services/zalo-personal/server.js"),
    "instagram:web": ("js", "services/instagram-web/server.js"),
}

#: 已知**由配置开关决定**的格子：(platform, mode, capability) → 开关路径。
#: 由门禁 ``test_capability_notes_are_live`` 逐条验证「翻这个开关，那个格子真的会变」，
#: 所以这里的说明不会像手写文档那样烂掉。
SWITCHED_CAPABILITIES: Dict[Tuple[str, str, str], str] = {
    ("line", "protocol", "send_media"): "platform_login.line.media.outbound",
    ("telegram", "protocol(companion)", "send_media"):
        "platform_login.telegram.companion_media",
}

#: 结构判定不了、且**不是我们没接而是对面没有**的能力——写清楚免得反复被问。
HARD_LIMITS: Dict[Tuple[str, str], str] = {
    ("line", "typing"): "okline 无 typing/presence 端点（协议层不可做，非未接）",
}


def _switch_on(config: Dict[str, Any], dotted: str) -> Dict[str, Any]:
    """返回把 ``dotted`` 路径置 True 的配置副本（浅层逐级建字典，足够本用途）。"""
    import copy
    out = copy.deepcopy(config or {})
    node = out
    parts = dotted.split(".")
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            node[p] = nxt
        node = nxt
    node[parts[-1]] = True
    return out


def _module_source(module: str) -> Optional[str]:
    """按点路径读模块源码（**不 import**，故缺可选依赖的机器上照样能判）。"""
    root = Path(__file__).resolve().parents[2]
    p = root / (module.replace(".", "/") + ".py")
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except OSError:
        return None


def _find_func(tree: ast.AST, dotted: str) -> Optional[ast.AST]:
    """按 ``Outer.method.inner`` 路径找函数/方法节点（支持任意层嵌套）；找不到 None。"""
    node: ast.AST = tree
    for part in dotted.split("."):
        found = None
        for child in ast.walk(node):
            if child is node:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)) and child.name == part:
                found = child
                break
        if found is None:
            return None
        node = found
    return node


def _forwards_media_type(func: ast.AST) -> bool:
    """函数体内是否存在「传 ``media_type=`` 且值不是字面空串」的调用。

    为什么用这个判据而不是「有没有调 make_message」：四条链的 payload 构造器根本不同
    （TG 走 ``tg_message_payload``、LINE/WA/Messenger 走 ``make_message``、A 线走
    ``_emit_inbox``），但**都必须把 media_type 算出来往下传**——不传，下游平台无关的
    识别层（``media_enrich``）就无从下手，客户发的图对 AI 即不存在。
    """
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "media_type":
                continue
            v = kw.value
            if isinstance(v, ast.Constant) and not str(v.value or "").strip():
                continue  # 显式传了空串 = 没接线
            return True
    return False


# ── JS 边车站点的文本判定 ────────────────────────────────────────────────────
# Node 边车的 ingest payload 在 JS 里构造，Python AST 够不着。文本判定弱于 AST，
# 但强于「把共享 ingest 路由当事实源」（那会把 Zalo/IG 没送的字段误判成已接线）。
# 判定范围收窄到「含 direction: 键的对象字面量」＝ingest payload 块，避免把
# send-media 出站 handler 里的 media_type 误当入站接线（真实假阳性来源，已验证）。

def _js_source(relpath: str) -> Optional[str]:
    p = Path(__file__).resolve().parents[2] / relpath
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except OSError:
        return None


def _mask_js_strings(src: str) -> str:
    """把 JS 字符串字面量**与注释**内容打成空格（保长度/换行），供括号配对与键定位。

    - 模板串 ``${WA_MEDIA_URL_BASE}/${fname}`` 里的花括号会干扰配对；
    - 注释里的 ``direction:"out"`` 之类字样会被键定位误命中（messenger 边车里
      真有这样的注释）——两者都必须掩掉。掩码不改变任何偏移：结构分析在掩码
    文本上做、取值时按同一偏移回原文切片。

    ⚠ **单/双引号串不跨行**是这里唯一的抗错锚（2026-08-20 实锤）：本掩码器不认
    正则字面量，messenger 边车 37k 处的 ``/can\\'t display/i`` 里那个转义引号
    曾被当成字符串开头，引号配对从此一路带偏 → ``direction:`` 命中从十几处塌成
    2 处，判据对文件后半段**彻底失明**（群接线明明写了却报「没接」）。遇到换行就
    判定「这个引号不是串的开头」并回滚本次掩码，把误伤锁死在一行内。
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in ("'", '"', "`"):
            q = ch
            body_start = i + 1
            j = body_start
            aborted = False
            while j < n:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == q:
                    break
                if c == "\n" and q != "`":
                    aborted = True  # 不是字符串（多为正则里的转义引号）
                    break
                j += 1
            if aborted:
                i += 1
                continue
            for k in range(body_start, min(j, n)):
                if src[k] != "\n":
                    out[k] = " "
            i = min(j, n) + 1
            continue
        elif ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        elif ch == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i < n:
                if src[i] == "*" and i + 1 < n and src[i + 1] == "/":
                    out[i] = " "
                    out[i + 1] = " "
                    i += 2
                    break
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _js_payload_blocks(src: str) -> List[Tuple[int, int]]:
    """返回所有「含 ``direction:`` 键的对象字面量」的 (open, close) 偏移对。"""
    import re as _re
    masked = _mask_js_strings(src)
    blocks: List[Tuple[int, int]] = []
    for m in _re.finditer(r"\bdirection\s*:", masked):
        # 向后回溯到本对象字面量的开括号（掩码文本上配对，模板串花括号已消毒）
        depth = 0
        open_at = -1
        for i in range(m.start() - 1, -1, -1):
            c = masked[i]
            if c == "}":
                depth += 1
            elif c == "{":
                if depth == 0:
                    open_at = i
                    break
                depth -= 1
        if open_at < 0:
            continue
        depth = 0
        close_at = -1
        for i in range(open_at, len(masked)):
            c = masked[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    close_at = i
                    break
        if close_at > open_at and (open_at, close_at) not in blocks:
            blocks.append((open_at, close_at))
    return blocks


def _js_value_expr(src: str, block: Tuple[int, int], key: str) -> Optional[str]:
    """取对象字面量里 ``key:`` 的值表达式**原文**（掩码文本定位、原文切片）。"""
    import re as _re
    masked = _mask_js_strings(src)
    open_at, close_at = block
    m = _re.search(r"\b%s\s*:" % _re.escape(key), masked[open_at:close_at])
    if not m:
        return None
    start = open_at + m.end()
    depth = 0
    end = close_at
    for i in range(start, close_at):
        c = masked[i]
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
        elif c == "," and depth == 0:
            end = i
            break
    return src[start:end].strip()


def _js_const_empty(expr: str) -> bool:
    """值表达式是否为**常量空串**（IG 的 ``chat_type: ""`` 形态＝显式没接线）。"""
    s = (expr or "").strip()
    return s in ('""', "''", "``") or not s


def _js_ingest_key_wired(src: str, key: str, *, must_contain: str = "") -> bool:
    """任一 ingest payload 块里 ``key`` 接了线（值非常量空串；可要求值含特定字面量）。"""
    for block in _js_payload_blocks(src):
        expr = _js_value_expr(src, block, key)
        if expr is None or _js_const_empty(expr):
            continue
        if must_contain and must_contain not in expr:
            continue
        return True
    return False


def inbound_media_wired(row_key: str) -> Optional[bool]:
    """该平台的入站 handler 有没有把媒体字段带进 payload。

    ``None`` = 登记的接线点找不到（多为重命名/重构）——**不等于不支持**，渲染成 ``?``
    并由门禁点名，逼人回来更新登记，而不是悄悄把一格翻成「不支持」。
    """
    site = INBOUND_SITES.get(row_key)
    if not site:
        return None
    module, dotted = site
    if module == "js":
        src = _js_source(dotted)
        if src is None:
            return None
        return _js_ingest_key_wired(src, "media_type")
    src = _module_source(module)
    if src is None:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    func = _find_func(tree, dotted)
    if func is None:
        return None
    return _forwards_media_type(func)


# ── 群会话三列（2026-08-19 群/频道 P0）─────────────────────────────────────────
# 「群消息进箱」的事实源按行分**三种**（与收媒体列同理：判定点必须在事实发生处）：
#   sink  Telegram 两档不靠 handler 标注——chat_key 即 chat_id，群/频道为负数，
#         由落库层 ``normalizer.infer_chat_type`` 启发式分流。判定＝**真调用**该函数
#         （可执行的活判据，函数改了立刻红）。
#   py    LINE protocol 在 handler 里显式 ``payload["chat_type"]="group"``（AST 判定）。
#   js    四个 Node 边车在 ingest payload 里带（或不带）``chat_type``（文本判定；
#         值必须真含 "group"——IG 的 ``chat_type:""`` 显式空串＝没接线，不算数）。

#: sink 启发式行 → 代表性群 chat_key 探针（负数 TG 群 id）。
GROUP_SINK_PROBES: Dict[str, str] = {
    "telegram:protocol": "-1001234567890",
    "telegram:protocol(companion)": "-1001234567890",
}


def _forwards_group_chat_type(func: ast.AST) -> bool:
    """函数体内是否把非空 ``chat_type`` 带进 payload（三种真实形态都认）：

    - 调用关键字：``make_message(..., chat_type=ct)``
    - 下标赋值：``payload["chat_type"] = "group"``（LINE protocol 的实际形态）
    - 字典字面量：``{"chat_type": _chat_type_str or "private"}``（A 线的实际形态）
    """
    def _nonempty(v: ast.AST) -> bool:
        return not (isinstance(v, ast.Constant) and not str(v.value or "").strip())

    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "chat_type" and _nonempty(kw.value):
                    return True
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "chat_type"
                        and v is not None and _nonempty(v)):
                    return True
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == "chat_type"
                        and _nonempty(node.value)):
                    return True
    return False


def group_inbound_wired(row_key: str) -> Optional[bool]:
    """该行的群消息能否被标注/分流进统一收件箱（``None``＝站点找不到＝渲染 ?）。"""
    if row_key in GROUP_SINK_PROBES:
        try:
            from src.inbox.normalizer import infer_chat_type
            platform = row_key.split(":", 1)[0]
            return infer_chat_type(platform, GROUP_SINK_PROBES[row_key]) == "group"
        except Exception:  # noqa: BLE001
            return None
    site = INBOUND_SITES.get(row_key)
    if not site:
        return None
    module, dotted = site
    if module == "js":
        src = _js_source(dotted)
        if src is None:
            return None
        return _js_ingest_key_wired(src, "chat_type", must_contain="group")
    src = _module_source(module)
    if src is None:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    func = _find_func(tree, dotted)
    if func is None:
        return None
    return _forwards_group_chat_type(func)


#: 群管理方法 → 中文标签（hasattr 判据，与 ``CAPABILITY_METHODS`` 同哲学）。
#: 当前全库只有 TG companion 的 ``invite_to_group``；其余方法名是 P2 的**预留契约**
#: （新 worker 按这些名字实现即自动进表，勿另起名）。
GROUP_ADMIN_METHODS: Dict[str, str] = {
    "invite_to_group": "拉人",
    "create_group": "建群",
    "kick_group_member": "踢人",
    "rename_group": "改名",
}


def worker_group_admin(worker: Any) -> List[str]:
    """内省 worker 实例的群管理能力标签列表（空列表＝无）。"""
    return [label for meth, label in GROUP_ADMIN_METHODS.items()
            if hasattr(worker, meth)]


def group_send_state(row: Dict[str, Any]) -> Optional[bool]:
    """「群内可发」＝群消息进箱 ∧ 发文本。

    推导而非独立判据：WA(@g.us)/LINE(gid)/TG(负 id) 的群地址**自描述**，send 与私聊
    同方法；Zalo 由边车群注册表自动解析 ThreadType（services/zalo-personal/
    group-registry.js，2026-08-19 修复——此前 Python 侧不传 chat_type、群回复按
    User 发）。看不见群（进箱 -）的平台谈不上群内回复，如实 ``-``。
    """
    if not row.get("available"):
        return None
    rg = row.get("recv_group")
    if rg is None:
        return None
    return bool(rg and (row.get("caps") or {}).get("send_text"))


def worker_capabilities(worker: Any) -> Dict[str, bool]:
    """内省一个 worker **实例**的能力（与编排器的 hasattr 判据同源）。"""
    return {cap: hasattr(worker, meth) for cap, meth in CAPABILITY_METHODS.items()}


def _instantiate(module: str, cls_name: str, config: Dict[str, Any]) -> Optional[Any]:
    """用假账号构造一个 worker 实例（四个 worker 的 ``__init__`` 都是纯赋值、无 IO）。

    必须走**实例**而非类：LINE 的 ``send_media`` 是按开关在 ``__init__`` 里条件绑定的
    （因为 ``owns_media()`` 判据就是实例上的 hasattr），只看类会漏掉这个语义。
    """
    try:
        import importlib
        mod = importlib.import_module(module)
        cls: Callable[..., Any] = getattr(mod, cls_name)
        return cls({"account_id": "_probe", "meta": {}}, config or {})
    except Exception:  # noqa: BLE001
        logger.debug("[platform_capabilities] 构造 %s.%s 失败", module, cls_name,
                     exc_info=True)
        return None


def capability_matrix(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按给定配置产出能力矩阵。

    返回 ``{"<platform>:<mode>": {"platform","mode","caps":{cap:bool},
    "available":bool}}``；``available=False`` 表示该 worker 类当前环境构造不出来
    （多为缺可选依赖），此时 caps 全 False 且调用方应显示「未知」而不是「不支持」。
    """
    cfg = config or {}
    out: Dict[str, Any] = {}
    for platform, mode, module, cls_name in WORKERS:
        key = "%s:%s" % (platform, mode)
        w = _instantiate(module, cls_name, cfg)
        site = INBOUND_SITES.get(key)
        row: Dict[str, Any] = {
            "platform": platform,
            "mode": mode,
            "class": "%s.%s" % (module, cls_name),
            "available": w is not None,
            "caps": worker_capabilities(w) if w is not None else {
                c: False for c in CAPABILITY_METHODS},
            # 入站与出站的**判据来源不同**（这个是源码接线，上面那些是方法内省），
            # 故单列一个键而不是混进 caps——省得后人以为它们同源。
            "recv_media": inbound_media_wired(key),
            # 群三列（2026-08-19）：进箱=接线判定（sink/py/js 三种事实源），
            # 管理=方法内省，可发=推导（见 group_send_state）。
            "recv_group": group_inbound_wired(key),
            "group_admin": (worker_group_admin(w) if w is not None else None),
            "inbound_site": ("%s::%s" % site) if site else "",
        }
        row["group_send"] = group_send_state(row)
        out[key] = row
    return out


def switched_cells(config: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, str]]:
    """哪些格子是「配置开关决定」的——**实测**出来的，不是声明出来的。

    做法：逐个把 ``SWITCHED_CAPABILITIES`` 里登记的开关翻开，看那个格子是否真的从
    False 变 True。翻了不变 = 登记已过期，本函数不会把它报成开关格（门禁会红）。
    """
    cfg = config or {}
    base = capability_matrix(cfg)
    found: Dict[str, Dict[str, str]] = {}
    for (platform, mode, cap), dotted in SWITCHED_CAPABILITIES.items():
        key = "%s:%s" % (platform, mode)
        row = base.get(key) or {}
        if not row.get("available"):
            continue
        if (row.get("caps") or {}).get(cap):
            continue  # 当前配置下已开，不算「待开」
        flipped = capability_matrix(_switch_on(cfg, dotted)).get(key) or {}
        if (flipped.get("caps") or {}).get(cap):
            found.setdefault(key, {})[cap] = dotted
    return found


#: 入站列的表头（判据来源与出站四列不同，见 ``inbound_media_wired``）
INBOUND_LABEL = "收图/语音（AI 可见）"

#: 群三列表头（判据来源见 ``group_inbound_wired`` / ``group_send_state`` /
#: ``GROUP_ADMIN_METHODS``）
GROUP_RECV_LABEL = "群消息进箱"
GROUP_SEND_LABEL = "群内可发"
GROUP_ADMIN_LABEL = "群管理 API"

#: 「自动回复设置」页拟人链关心的两项能力（键与 ``CAPABILITY_METHODS`` 对齐）
HUMANIZE_CAPS = ("mark_read", "typing")


def humanize_caps_by_platform(
    config: Optional[Dict[str, Any]] = None, *,
    matrix: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """按**平台**收敛 mark_read / typing 支持度（给 /reply-settings 的能力清单）。

    背景：设置页的「回复前先标已读 / 正在输入」是全局开关，但底层能力按平台分裂；
    此前页面 hint 手写「当前 Telegram 协议号支持」——已经过时（WhatsApp/LINE 早就
    支持已读）。手写清单必然烂掉，故这里从 ``capability_matrix``（与编排器运行时
    hasattr 判据同源）现算，UI 直接渲染，能力变了清单自动跟。

    返回 ``{platform: {cap: {"state": ..., "hard": bool}}}``；state ∈

    - ``ok``          该平台所有可构造模式都支持
    - ``partial``     部分模式支持（同平台两档不一致，如实陈述不硬判）
    - ``unsupported`` 没有模式支持
    - ``unknown``     所有 worker 都构造不出（多为缺可选依赖）——**不等于不支持**

    ``hard=True`` = ``HARD_LIMITS`` 登记的协议层硬限制（对面没有，不是我们没接）。
    ``matrix`` 可注入（门禁用假矩阵钉纯逻辑），缺省现算。
    """
    rows = matrix if matrix is not None else capability_matrix(config)
    votes: Dict[str, Dict[str, List[bool]]] = {}
    for row in (rows or {}).values():
        platform = str(row.get("platform") or "")
        if not platform:
            continue
        bucket = votes.setdefault(platform, {c: [] for c in HUMANIZE_CAPS})
        if not row.get("available"):
            continue  # 构造不出的模式不投票；全构造不出 → 零票 → unknown
        caps = row.get("caps") or {}
        for c in HUMANIZE_CAPS:
            bucket[c].append(bool(caps.get(c)))
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for platform, buckets in votes.items():
        entry: Dict[str, Dict[str, Any]] = {}
        for cap, flags in buckets.items():
            if not flags:
                state = "unknown"
            elif all(flags):
                state = "ok"
            elif any(flags):
                state = "partial"
            else:
                state = "unsupported"
            entry[cap] = {"state": state,
                          "hard": (platform, cap) in HARD_LIMITS}
        out[platform] = entry
    return out

# ── 代码接线 × 实际到货 对照 ─────────────────────────────────────────────────
# 静态矩阵回答「代码支不支持」，生产库回答「实际有没有到货」。两者**并排**才有诊断力：
# 一致＝真健康；「接了没到货」多半是上游或配置；「没接却有货」说明判据漏了某条旁路。
# 本次会话就是靠这种交叉验伪抓到过两个结论（TG 出站看似断了其实是记账口径不同；
# 「第二张照片会撤回第一张」纯属虚惊），把它固化成工具比每次临时写探针可靠。

#: 已解释的差异：(platform, 维度) → 原因。登记要带原因（仿仓库既有的
#: ``_ACCEPTED_DUP_IDS``），且由门禁 ``test_accepted_mismatches_not_stale`` 防过期。
ACCEPTED_MISMATCHES: Dict[Tuple[str, str], str] = {
    ("telegram", "send_media"):
        "两档不同且生产跑的是 companion：该 worker 无 send_media，"
        "但 A 线 pyrogram 原生直发媒体（绕过编排器），故实际有货",
}

#: 样本低于此数不下判断（零星几条消息说明不了「到没到货」）
MIN_LIVE_SAMPLE = 20


def code_flags(matrix: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """把按 (platform, mode) 的矩阵收敛成按 platform 的三态标记。

    同平台两档不一致 → ``mixed``（Telegram 的 protocol vs companion 就是这种），
    此时**不做一致性判断**、只如实说明分档不同——硬判会造出假告警。
    """
    per: Dict[str, Dict[str, list]] = {}
    for row in matrix.values():
        p = per.setdefault(row["platform"], {"send_media": [], "recv_media": []})
        p["send_media"].append(bool((row.get("caps") or {}).get("send_media")))
        rm = row.get("recv_media")
        if rm is not None:
            p["recv_media"].append(bool(rm))
    out: Dict[str, Dict[str, str]] = {}
    for platform, dims in per.items():
        out[platform] = {}
        for dim, vals in dims.items():
            if not vals:
                out[platform][dim] = "unknown"
            elif all(vals):
                out[platform][dim] = "Y"
            elif not any(vals):
                out[platform][dim] = "N"
            else:
                out[platform][dim] = "mixed"
    return out


def reconcile(
    flags: Dict[str, Dict[str, str]],
    live: Dict[str, Dict[str, int]],
    *,
    min_sample: int = MIN_LIVE_SAMPLE,
    accepted: Optional[Dict[Tuple[str, str], str]] = None,
) -> List[Dict[str, Any]]:
    """代码标记 × 生产读数 → 逐 (平台, 维度) 的判词（纯函数，便于门禁钉）。

    ``live[platform]`` 需含 ``in_total/in_media/out_total/out_media``。
    判词 ``verdict``：

    - ``ok``            代码说支持、实际也有货
    - ``no_traffic``    样本不足，不下判断（**不是**问题）
    - ``dry``           代码说支持、样本够却零到货 → 查上游/配置
    - ``unexpected``    代码说不支持却有货 → 判据漏了某条旁路（或矩阵写错了）
    - ``mixed``         同平台两档不一致，如实陈述不判
    - ``explained``     命中 ``ACCEPTED_MISMATCHES``，附原因
    - ``out_of_scope``  生产库里有这个平台的数据，但它不在矩阵内（如内置 web 测试聊天）

    ⚠ **正面证据优先于样本量**：只要真有媒体到货就判 ``ok``/``unexpected``，不管总量
    多少——「发出去过 5 条」已经证明链路通了。样本阈值只用来拦**否定**结论（没到货
    到底是坏了还是压根没流量），否则低流量平台会被误报成「样本不足」而丢掉正面信号。
    """
    acc = ACCEPTED_MISMATCHES if accepted is None else accepted
    rows: List[Dict[str, Any]] = []
    for platform in sorted(set(flags) | set(live)):
        st = live.get(platform) or {}
        for dim, tot_k, med_k in (("recv_media", "in_total", "in_media"),
                                  ("send_media", "out_total", "out_media")):
            flag = (flags.get(platform) or {}).get(dim, "unknown")
            total = int(st.get(tot_k) or 0)
            media = int(st.get(med_k) or 0)
            item = {"platform": platform, "dim": dim, "code": flag,
                    "total": total, "media": media, "reason": ""}
            why = acc.get((platform, dim))
            if flag == "unknown" and platform not in flags:
                # 生产库里有数据但矩阵里没有这个平台（内置 web 测试聊天等）——
                # 如实说「不在范围」，别拿它凑一个看着很健康的 OK。
                item["verdict"] = "out_of_scope"
            elif why:
                item.update(verdict="explained", reason=why)
            elif flag == "mixed":
                item["verdict"] = "mixed"
            elif media > 0:
                # 正面证据先于样本量：真到过货就说明链路通，与总量无关
                item["verdict"] = "unexpected" if flag == "N" else "ok"
            elif total < min_sample:
                item["verdict"] = "no_traffic"
            elif flag == "Y":
                item["verdict"] = "dry"
            else:
                item["verdict"] = "ok"   # 代码说不支持、也确实没货 = 自洽
            rows.append(item)
    return rows

__all__ = [
    "CAPABILITY_METHODS",
    "CAPABILITY_LABELS",
    "HUMANIZE_CAPS",
    "humanize_caps_by_platform",
    "INBOUND_SITES",
    "INBOUND_LABEL",
    "GROUP_RECV_LABEL",
    "GROUP_SEND_LABEL",
    "GROUP_ADMIN_LABEL",
    "GROUP_ADMIN_METHODS",
    "GROUP_SINK_PROBES",
    "group_inbound_wired",
    "group_send_state",
    "worker_group_admin",
    "ACCEPTED_MISMATCHES",
    "MIN_LIVE_SAMPLE",
    "code_flags",
    "reconcile",
    "inbound_media_wired",
    "WORKERS",
    "SWITCHED_CAPABILITIES",
    "HARD_LIMITS",
    "capability_matrix",
    "switched_cells",
    "worker_capabilities",
]
