"""入站媒体 → 可喂 AI 的文本（图片 Vision / 语音 ASR / 视频抽帧）。

平台无关的**共享识别层**：原生 Telegram A 线、协议直发线（``protocol_autoreply``）、
收件箱全自动草稿链都可复用，避免"各写一遍"。

背景（本模块补的坑）：协议直发线 ``protocol_autoreply`` 此前只看入站 ``text``——
对方发来纯图片/纯语音（无 caption）时，直发线要么把它判为 ``incomplete`` 早退、
要么把 ``[图片]`` 占位喂给 AI 让其搪塞"我看不了图"。只有会话被收件箱"全自动
auto_ai"托管、走 ``autodraft_helpers`` 那条链时才有识图/转写/视频理解。本模块把
那套识别能力抽成平台无关函数，让直发线也能识别，与 Telegram / 全自动链口径一致。

铁律：所有识别失败一律软降级到占位符或原 caption，**绝不抛异常、绝不阻断回复主链**。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_IMAGE_KINDS = {"image", "photo", "sticker"}
_VOICE_KINDS = {"voice", "audio"}
_VIDEO_KINDS = {"video", "video_note", "animation", "gif"}

# 无描述时的占位文案（与 protocol_bridge / inbound_enrich 口径一致）
_PLACEHOLDER = {
    "image": "[图片]", "photo": "[图片]", "sticker": "[贴纸]",
    "voice": "[语音]", "audio": "[语音]",
    "video": "[视频]", "video_note": "[视频]", "animation": "[动图]", "gif": "[动图]",
    "document": "[文件]", "file": "[文件]",
}

# 只含媒体占位、无实质文本 → 视为"该识别但还没识别"。识别文本前缀（[图片内容] 等）
# 不算占位（那已经是识别结果）。
_PLACEHOLDER_TEXTS = frozenset({
    "[图片]", "[贴纸]", "[语音]", "[视频]", "[动图]", "[gif]", "[GIF]",
    "[文件]", "[媒体]", "[表情]", "[动态表情]",
})


def media_placeholder(media_type: str) -> str:
    return _PLACEHOLDER.get(str(media_type or "").lower(), "[媒体]")


def is_placeholder_only(text: str) -> bool:
    """入站文本是否只是裸媒体占位（无实质内容）——需要识别补全的信号。

    识别结果（``[图片内容] …`` / ``[视频内容] …`` 等带描述的）不算占位。
    """
    t = str(text or "").strip()
    if not t:
        return True
    if t in _PLACEHOLDER_TEXTS:
        return True
    # 形如 [xxx] 的短裸占位（≤8 字符、不含描述）
    return t.startswith("[") and t.endswith("]") and len(t) <= 8


# --- 识别产物质量闸门（P0 2026-08-19，「乱码识图」事故沉淀） -------------------
# 事故：客户发来文字密集的图（机械键盘 + 屏幕上的看板/界面），生产 vision prompt 的
# 「逐字抄录」条款让 VLM 把键帽（F2 F3 QWERTY 3# 4$）、看板卡片标题、界面话术整段
# 抄进描述 → 坐席端识别行 + 译文行两面文字墙，AI 上下文/记忆抽取被碎片污染。
# prompt 已改为分类输出（票据仍逐字、普通图只给 1-2 句），这里是模型不听话时的
# 兜底保险。判定**保守**：宁可放过，绝不误杀票据逐字抄录（字段:值 结构、CJK 标签、
# 长值 token 不会命中下面任何一条）。

#: 识别描述在消息 text 里的标记前缀（写入方：本模块 / telegram_client / inbound_video）
#: #143（0902）：贴纸识别文本改标 ``[贴纸内容]``（曾与图片共用 [图片内容]，贴纸性
#: 在正文/历史里丢失 → 表情包被当真实照片评论），同步进剥离表。
MEDIA_DESC_MARKERS = ("[图片内容]", "[视频内容]", "[贴纸内容]")

#: 闸门命中时的替换文案——诚实告知「有字但没法可靠抄」，并钉住 AI 不要臆测。
GARBLED_DESC_NOTE = "图中文字零散（键盘按键/界面元素等），未能可靠识别；不要臆测图片内容"

_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")

#: 语音转写行的行首数据标记（telegram_client 落库格式「[语音转录] 正文」）。
_VOICE_TRANSCRIPT_MARK = "[语音转录]"


def strip_media_desc(text: str) -> str:
    """剥掉消息文本里的识别描述段，只留客户自己的话（caption）。

    识别描述是**系统自产内容**，两类下游都不该把它当客户的话消费：
    - 翻译源文（前端 ``_xlateSrcText`` / 服务端 ``inbound_translate`` 同口径）——
      译它＝把碎片 OCR 再造伪句还烧字符；
    - 记忆抽取（``ai_client.extract_memory_bullets`` / ``extract_heuristic_facts``）——
      聊天截图里被抄录的「我是XX」会被当成**用户本人**事实入库（Phase8 幻觉同族）。

    含 ``[图片内容]`` / ``[视频内容]`` 标记 → 只取标记前的 caption 段（可为空串＝
    无客户文字）；无标记（普通文本 / 语音转写正文）原样返回，行为零变化。

    P0-V（2026-08-19，接 FYI 移交）：行首 ``[语音转录]`` 数据标记同属系统自产——
    带着送翻译会污染源语检测（6 个 CJK 字把短外语转写判成 zh → 与 zh 目标撞
    identity → 该行**永远得不到自动译文**），送记忆抽取则把标记当叙述。只剥
    **行首一次**（正文里出现同字样是客户的话，不动）。与前端 ``_xlateSrcText``
    同口径（两处必须同改）。
    """
    t = str(text or "")
    ts = t.lstrip()
    if ts.startswith(_VOICE_TRANSCRIPT_MARK):
        t = ts[len(_VOICE_TRANSCRIPT_MARK):].lstrip()
    idx = -1
    for mk in MEDIA_DESC_MARKERS:
        i = t.find(mk)
        if i >= 0 and (idx < 0 or i < idx):
            idx = i
    if idx < 0:
        return t
    return t[:idx].strip()


# 兼容别名（2026-08-19 上午同日改名，尚未进任何重启批次；留别名防并行线引用旧名）
strip_media_desc_for_translation = strip_media_desc

# 识别描述首行的类型标记（P1 2026-08-19）：prompt 要求 VLM 首行输出「类型=A|B|C」
# （A=单据逐字 / B=聊天截图概括 / C=普通图简述）。标记**保留在落库文本里**当结构载体：
# 前端解析成类型徽标并从显示正文剥离；stats 按类型分布计数；AI 上下文原样可见
# （对回复 LLM 是有用的分类信号）。无标记＝旧产出/模型未遵循 → 一切按未分类走。
_DESC_TYPE_RE = None  # 懒编译（模块导入零 re 开销）


def parse_desc_type(desc: str) -> Tuple[str, str]:
    """解析识别描述首行的 ``类型=A|B|C`` 标记 → ``(大写代码, 去标记正文)``。

    无标记 / 标记不在首行 → ``("", 原文)``。容忍全角 ＝／：、小写代码与行尾句读。
    """
    global _DESC_TYPE_RE
    import re
    if _DESC_TYPE_RE is None:
        _DESC_TYPE_RE = re.compile(
            r"^\s*(?:类型|type)\s*[=＝:：]\s*([ABCabc])\s*[。．.、]?[ \t]*\r?\n?")
    t = str(desc or "")
    m = _DESC_TYPE_RE.match(t)
    if not m:
        return "", t
    return m.group(1).upper(), t[m.end():].strip()


_DESC_LINE_TERMINALS = "。！？；!?;…"


def flatten_desc_line(desc: str) -> str:
    """识别描述压成**单行**（#143 C-补，0902 工单「切中文」真机制）。

    VLM 按 prompt「简洁分条」原样带换行落库：``[图片内容] 1. 可见物体：…\\n\\n2. 无票据/
    订单/证件/表单/聊天记录``——语言证据剥离正则只剥带标记的**首行**，续行「2. 无票据…」
    以裸中文漏进 ``classify_evidence``/``vote_language`` → 英文会话被判 zh 强证据，
    pin/expected=zh 把英文原稿整段改成中文（HXP9YD 日志 13334/13339/13351）。
    写入侧收口：换行 → 「；」（上一段已以句读结尾则只留空格），首行 ``类型=X`` 标记
    保留为行首 token（``parse_desc_type`` / 前端 ``_DESC_TYPE_RE`` 都容忍空格分隔）。
    三个写入点（本模块 / telegram_client._get_image_content / inbound_video）同口径；
    读取侧 ``lang_policy`` 另有存量兼容剥离。纯函数，空入空出。
    """
    t = str(desc or "").replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in t:
        return t.strip()
    code, body = parse_desc_type(t)
    pieces = [ln.strip() for ln in body.split("\n")]
    pieces = [p for p in pieces if p]
    out = ""
    for p in pieces:
        if not out:
            out = p
        elif out[-1] in _DESC_LINE_TERMINALS:
            out = f"{out} {p}"
        else:
            out = f"{out}；{p}"
    if code:
        return f"类型={code} {out}".strip()
    return out


def build_ask_image_prompt(question: str) -> str:
    """「问这张图」的 VLM 提问 prompt（P2 2026-08-19，ask-image 路由消费）。

    与识图描述链分离：这是坐席对单张图的即席提问（类型感知预设或自由输入），
    与描述链共守同一条纪律——只据画面作答、看不到就直说，绝不编造。
    """
    q = " ".join(str(question or "").split())[:200]
    return (
        "只根据这张图片回答下面的问题，不要编造：图里没有或看不清的信息就直说"
        "「图中看不到」。直接给答案，不要复述问题，回答用中文，不超过 80 字。\n"
        f"问题：{q}"
    )


def desc_looks_garbled(desc: str) -> bool:
    """识图/识视频产出是否为「碎片文字汤」（键盘键帽 / 界面元素逐字抄录那类）。

    只抓两类高置信汤，其余一律放行：
    ① 键盘硬信号：qwerty/asdf/zxcv 键盘行出现 ≥2 行，或 F1..F12 功能键连片；
    ② 比例信号：长文本里「键位型碎片 token」（纯符号 / 字母+数字 ≤4 字符 /
       数字+符号）既多（≥10 个）又占比高（≥40%）。
    普通中文描述（整句连写＝超长 token）、英文散文（短虚词不计碎片）、票据逐字
    抄录（CJK 字段标签 + 长值）都不会命中——有金标回归钉住（test_media_desc_guard）。
    """
    import re

    t = str(desc or "").strip()
    if len(t) < 80:      # 短描述没有刷屏危害，不判（新 prompt 下正常产出都在这档）
        return False
    low = t.lower()
    compact = re.sub(r"[\s\|,，、/]+", "", low)
    rows = sum(1 for r in _KEYBOARD_ROWS if r in compact)
    fkeys = len(re.findall(r"\bf(?:1[0-2]|[1-9])\b", low))
    if rows >= 2 or (rows >= 1 and fkeys >= 4) or fkeys >= 8:
        return True
    tokens = [x for x in re.split(r"[\s，,、;；|/·]+", t) if x]
    if len(tokens) < 15:
        return False
    junk = 0
    for tok in tokens:
        if re.fullmatch(r"[\W_]+", tok):                    # 纯符号（= - <>? {}[]）
            junk += 1
        elif re.fullmatch(r"[A-Za-z]{1,2}\d{1,2}", tok):    # F2 / E3 / K8 类键位
            junk += 1
        elif re.fullmatch(r"\d{1,2}[\W_]{1,2}", tok):       # 3# / 4$ / 9( 类符号键
            junk += 1
    return junk >= 10 and (junk / len(tokens)) >= 0.4


# --- 懒建 ASR transcriber（宿主没建时的兜底） ---------------------------------
# 背景：TelegramClient.voice_transcriber 只在进程启动时按当时的
# voice_recognition.enabled 初始化；用户随后用自检卡/运营开关热开 ASR 时，
# 已运行进程里它仍是 None → 语音/视频音轨永远不转写，除非重启。
# 这里按当前配置懒建一个并缓存（配置指纹变了就重建），让"一键开启"即时生效。
_LAZY_VTR: Any = None
_LAZY_VTR_KEY: Optional[str] = None


def media_degrade_reply_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """「识别失败 → 降级诚实自动回」开关（``inbox.auto_draft.media_degrade_reply``）。

    默认 **False**＝维持 2026-08-17 无兜底纪律：图片/语音识别失败一律拦下不回。
    置 True 时，两条自动回复链（协议直发 protocol_autoreply / 收件箱拟稿
    autodraft_helpers）对识别失败的图片/语音**不再扣留**，改为携带「无描述媒体」
    上下文照常生成——ai_client 媒体块对无 desc 媒体自带「自然承认收到 + 温和
    追问对方想表达什么」话术（诚实降级，绝不装懂，非垫场句：模型明确知道自己
    没看到内容）。2026-08-22 老板「不要再有小问题掐断全自动」拍板后进桌面种子 +
    A 类基线（客户包默认开）；**服务器实例代码默认关**保持无兜底纪律，要开走
    overlay。判定单点在此，两链共用，勿各自再读一遍 config。
    """
    try:
        return bool((((config or {}).get("inbox") or {}).get("auto_draft")
                     or {}).get("media_degrade_reply", False))
    except Exception:
        return False


# --- 拟稿等图 + 盲断言闸门（2026-08-23「把人看成猫」事故 P0） --------------------
# 事故机制（网关流水实锤）：照片行落库与 media_ref/媒体文件就绪之间有秒级窗口
# （边车先落消息行、后补媒体），拟稿抢在识别前生成 → prompt 里只有「[图片]」占位，
# ref 为空时连降级分支都进不去（无媒体块无诚实指令），LLM 自由发挥出
# 「哈哈，这图是你家猫吗」；识图两分钟后才被下一轮补上。修法两层：
# ① 拟稿对「说有图但还看不到」的行等一等（media_wait_sec 预算内等 ref/文件/
#    别的链路回写的描述），等到才生成——degrade 只吃「真识别失败」，不再吃
#    「还没来得及识」；
# ② 出稿硬校验（blind_image_assertion）：无描述的图片轮，回复不得断言画面内容
#    ——降级指令是软约束，模型违约就整稿换成诚实追问（honest_image_ask）。

#: 拟稿等图的缺省预算（秒）。生产观测：边车补 ref/文件通常 2~5s、识图 1.5~2s，
#: 20s 足够覆盖 P95 且远小于「先瞎回再露馅」的社交代价；0=关闭等待（旧行为）。
DEFAULT_MEDIA_WAIT_SEC = 20.0

#: 等待轮询步长（秒）。测试可 monkeypatch 调小加速。
_WAIT_TICK_SEC = 1.5


def media_wait_sec_from_cfg(config: Optional[Dict[str, Any]]) -> float:
    """拟稿前等图片就绪的秒数（``inbox.auto_draft.media_wait_sec``，默认 20，0=关）。

    夹 [0, 60]：等太久=客户看到「已读不回」，比慢几秒更伤。"""
    try:
        raw = (((config or {}).get("inbox") or {}).get("auto_draft")
               or {}).get("media_wait_sec")
        if raw is None:
            return DEFAULT_MEDIA_WAIT_SEC
        v = float(raw)
    except Exception:
        return DEFAULT_MEDIA_WAIT_SEC
    return max(0.0, min(v, 60.0))


# 同图识别去重锁（进程内）：连发「图 + 紧跟一句话」会触发两个拟稿轮，各自等图后
# 都去调 VLM = 同图双烧 GPU。首个拟稿轮占坑识别并回写消息行，后来者只轮询等
# 回写结果。TTL 兜底防异常路径漏清（识别+回写远快于 TTL）。
_DESC_INFLIGHT: Dict[str, float] = {}
_INFLIGHT_TTL_SEC = 90.0


def try_mark_desc_inflight(key: str) -> bool:
    """占「这条媒体的识别正在进行」坑；已有未过期标记返回 False（改为轮询等结果）。"""
    now = time.time()
    ts = _DESC_INFLIGHT.get(key)
    if ts and now - ts < _INFLIGHT_TTL_SEC:
        return False
    _DESC_INFLIGHT[key] = now
    return True


def clear_desc_inflight(key: str) -> None:
    _DESC_INFLIGHT.pop(key, None)


# 盲断言检测：只在「图片轮且无识别描述」时消费——此时回复对画面内容的任何断言
# /猜测/夸赞都是装懂。判定分两步：先认「诚实标记」（承认没看到/加载失败＝合规
# 降级话术，放行）；再抓「断言画面」的高置信句式。有描述时不该调本函数
# （描述里真提到猫时说猫是对的）。
_BLIND_RES: Optional[list] = None   # 懒编译
_HONESTY_RE = None


def _blind_res():
    global _BLIND_RES, _HONESTY_RE
    if _BLIND_RES is not None:
        return _BLIND_RES, _HONESTY_RE
    import re
    _HONESTY_RE = re.compile(
        r"看不清|没看清|看不到|看不了|打不开|加载不出|没加载|加载失败|没显示"
        r"|显示不出|没收到|收不到|没能识别|识别不了|再发一次|重新发一?[张遍次]"
        r"|(?i:didn'?t\s+load|not\s+load|can'?t\s+(?:see|open|view)"
        r"|won'?t\s+load|not\s+showing|didn'?t\s+come\s+through)")
    _BLIND_RES = [
        # 「这/那(张)图/照片…是/有/拍的/看起来/好像/应该是」——断言或猜测画面
        re.compile(r"[这那]\s*[张幅个]?\s*(?:图片?|照片|相片)\s*[里上中]?"
                   r"\s*[^，。！？!?\n]{0,6}?(?:是|有|拍的|画的|写的|看起来|好像|应该是)"),
        # 「图/照片 里/上/中 的|是|有」
        re.compile(r"(?:图片?|照片|相片)[里上中](?:的|是|有|那|这)"),
        # 「看到/看见…图/照片」——声称看过
        re.compile(r"(?:看到|看见|瞅见|瞧见)(?:你发?的|这|那)?"
                   r"[^，。！？!?\n]{0,8}(?:图片?|照片|相片)"),
        # 「拍得真好/拍的挺美」——夸构图=声称看过
        re.compile(r"拍[得的](?:真|好|很|挺|太)"),
        # en：断言/夸赞画面
        re.compile(r"(?i)\b(?:this|that|the)\s+(?:pic(?:ture)?|photo|image|shot)"
                   r"\s+(?:is|of|looks|shows|seems)"),
        re.compile(r"(?i)\bis\s+(?:this|that)\s+your\s+\w+"),
        re.compile(r"(?i)\b(?:nice|great|beautiful|lovely|cute|cool)"
                   r"\s+(?:pic(?:ture)?|photo|shot|image)\b"),
    ]
    return _BLIND_RES, _HONESTY_RE


def blind_image_assertion(text: str) -> bool:
    """无识别描述时，回复是否在断言/猜测/夸赞图片内容（＝装懂，须拦）。

    保守两步：句中带「看不清/加载失败」类诚实标记 → 一律放行（那是合规降级
    话术）；否则命中任一断言句式才判真。宁可放过含糊句，不误杀正常回复。"""
    t = str(text or "").strip()
    if not t:
        return False
    res, honesty = _blind_res()
    if honesty.search(t):
        return False
    return any(r.search(t) for r in res)


#: 诚实追问话术池（盲断言拦下后的整稿替换）。变体按会话 crc32 确定性轮换：
#: 同会话稳定（缓存/复读判定友好）、跨会话不千篇一律。
_HONEST_ASK_POOL = {
    "zh": [
        "咦，这张图我这边一直加载不出来，你直接跟我说说拍的是啥呗？",
        "图好像没加载出来，我这边看不到内容——你发的是什么呀？",
        "收到图啦，不过我这边显示不出来，你给我描述一下呗？",
    ],
    "en": [
        "Hmm, the picture won't load on my end — what's in it?",
        "I got the photo but it's not showing for me. What did you send?",
        "The image isn't loading here — tell me what it is?",
    ],
}


def honest_image_ask(lang: str, seed: str = "") -> str:
    """按回复语言取一条诚实追问（非 zh 一律走 en；出站自动翻译层会再对齐客户语言）。"""
    import zlib
    pool = _HONEST_ASK_POOL[
        "zh" if str(lang or "").lower().startswith("zh") else "en"]
    return pool[zlib.crc32(str(seed or "").encode("utf-8")) % len(pool)]


def lazy_voice_transcriber(config: Optional[Dict[str, Any]]) -> Any:
    """配置启用 ASR 时返回（懒建+缓存的）transcriber；未启用/建失败返回 None。"""
    global _LAZY_VTR, _LAZY_VTR_KEY
    vr = (config or {}).get("voice_recognition") or {}
    if not vr.get("enabled", False):
        return None
    key = repr(sorted((str(k), repr(v)) for k, v in vr.items()))
    if _LAZY_VTR is not None and _LAZY_VTR_KEY == key:
        return _LAZY_VTR
    try:
        from src.voice_transcriber import VoiceTranscriberFactory
        _LAZY_VTR = VoiceTranscriberFactory.create_transcriber(vr)
        _LAZY_VTR_KEY = key
        logger.info("[media_enrich] ASR transcriber 懒建成功（热开关生效，无需重启）")
        return _LAZY_VTR
    except Exception:
        logger.debug("[media_enrich] ASR transcriber 懒建失败", exc_info=True)
        _LAZY_VTR = None
        _LAZY_VTR_KEY = None
        return None


def _resolve_local_path(media_ref: str) -> Optional[str]:
    """把 media_ref 解析成本进程可读的本地文件绝对路径；解析不到返回 None。

    优先 protocol 落地的 ``/static/protocol_media/...`` URL，其次通用解析
    （绝对路径 / file:// / 相对）。远程 http(s) URL 本层不下载 → None。
    """
    ref = str(media_ref or "").strip()
    if not ref:
        return None
    try:
        from src.integrations.protocol_bridge import static_media_ref_to_path
        p = static_media_ref_to_path(ref)
        if p and os.path.isfile(p):
            return p
    except Exception:
        logger.debug("[media_enrich] static_media_ref_to_path 解析失败", exc_info=True)
    try:
        from src.inbox.media_resolver import resolve_media_path
        return resolve_media_path({"media_ref": ref})
    except Exception:
        logger.debug("[media_enrich] resolve_media_path 解析失败", exc_info=True)
        return None


async def _describe_image(path: str, cfg: Dict[str, Any]) -> str:
    vision_cfg = (cfg or {}).get("vision") or {}
    if not vision_cfg.get("enabled", False):
        return ""
    try:
        from src.vision_client import VisionClient, has_any_vision_backend
    except Exception:
        return ""
    if not has_any_vision_backend(vision_cfg, vision_cfg):
        return ""
    text, _tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
        vision_cfg, vision_cfg, str(path), prompt=vision_cfg.get("prompt"))
    return (text or "").strip()[:2000]


async def _transcribe_voice(path: str, cfg: Dict[str, Any], voice_transcriber: Any) -> str:
    if voice_transcriber is None:
        return ""
    lang = str(((cfg or {}).get("voice_recognition") or {}).get("language", "auto") or "auto")
    txt = await voice_transcriber.transcribe_voice_message(str(path), lang)
    return (txt or "").strip()


async def _understand_video(path: str, cfg: Dict[str, Any], voice_transcriber: Any) -> str:
    from src.ai.inbound_video import understand_video_file
    out = await understand_video_file(
        str(path),
        vision_config=(cfg or {}).get("vision") or {},
        voice_transcriber=voice_transcriber,
        speech_emotion_config=(cfg or {}).get("speech_emotion") or {},
        voice_recognition_config=(cfg or {}).get("voice_recognition") or {},
    )
    return (out or "").strip()


def _miss_reason(media_type: str, cfg: Dict[str, Any]) -> str:
    """这条媒体为什么没被看懂——把三种同形不同因的处境分开（见 media_enrich_stats）。

    ``disabled``＝开关没开（运营信号，不是故障）；``no_backend``＝开了但后端没接上
    （可自助修）；``failed``＝后端在但识别失败/返空（看日志）。
    """
    try:
        from src.companion.media_capability import asr_backend_ready, vision_backend_ready

        if media_type in _IMAGE_KINDS or media_type in _VIDEO_KINDS:
            if not ((cfg.get("vision") or {}).get("enabled", False)):
                return "disabled"
            return "failed" if vision_backend_ready(cfg) else "no_backend"
        if media_type in _VOICE_KINDS:
            if not ((cfg.get("voice_recognition") or {}).get("enabled", False)):
                return "disabled"
            return "failed" if asr_backend_ready(cfg) else "no_backend"
    except Exception:
        logger.debug("[media_enrich] 归因失败（忽略）", exc_info=True)
        return "unknown"
    return "unsupported"


def _record_enrich(media_type: str, cfg: Dict[str, Any], *,
                   understood: bool, reason: str = "", desc_type: str = "") -> None:
    """识别结果计数（best-effort，绝不影响主链）。``desc_type``＝首行类型标记（可空）。"""
    try:
        from src.inbox.media_enrich_stats import get_media_enrich_stats

        st = get_media_enrich_stats()
        if understood:
            st.record_understood(media_type, desc_type=desc_type)
        else:
            st.record_miss(media_type, reason or _miss_reason(media_type, cfg))
    except Exception:
        logger.debug("[media_enrich] 计数失败（忽略）", exc_info=True)


async def enrich_inbound_media_text(
    *,
    media_type: str,
    media_ref: str,
    caption: str = "",
    config: Optional[Dict[str, Any]] = None,
    voice_transcriber: Any = None,
    wait_file_sec: float = 0,
) -> Tuple[str, str]:
    """识别入站媒体，返回 ``(供 AI 的文本, 识别描述)``。

    - 图片/贴纸 → VisionClient（Ollama→智谱链），需 ``vision.enabled`` + 后端可用。
    - 语音/音频 → ``voice_transcriber.transcribe_voice_message``（转写即"对方说的话"）。
    - 视频/GIF → ``understand_video_file``（抽关键帧 + 音轨 ASR/SER）。
    - 识别不出 / 无后端 / 远程未下载 → 文本回落 ``caption`` 或占位符，描述空串。
    - ``wait_file_sec`` > 0：media_ref 一时解析不到本地文件（边车下载中）时，
      在预算内轮询等文件就绪再识别——「回复抢跑、识图迟到」的共享层修法。

    全程软失败，绝不抛异常。
    """
    cfg = config or {}
    mt = str(media_type or "").strip().lower()
    cap = str(caption or "").strip()
    if not mt and not media_ref:
        return cap, ""

    local = _resolve_local_path(media_ref)
    if not local and media_ref and wait_file_sec > 0:
        deadline = time.monotonic() + max(0.0, min(float(wait_file_sec), 60.0))
        while not local and time.monotonic() < deadline:
            await asyncio.sleep(_WAIT_TICK_SEC)
            local = _resolve_local_path(media_ref)
    tmp_download: Optional[str] = None
    try:
        # 官方通道镜像常把 CDN https 写进 media_ref（IG/Messenger/Zalo）；
        # 本地解析不到时，按 media.remote_fetch（默认关，SSRF 护栏）受控下载再识别。
        if not local:
            local, tmp_download = await _maybe_fetch_remote(media_ref, mt, cfg)
        if not local:
            if mt:
                _record_enrich(mt, cfg, understood=False, reason="unresolved")
            return (cap or media_placeholder(mt)), ""

        # 宿主（TelegramClient）启动时未建 transcriber、但配置已（热）启用 → 懒建兜底
        if voice_transcriber is None and mt in (_VOICE_KINDS | _VIDEO_KINDS):
            voice_transcriber = lazy_voice_transcriber(cfg)

        desc = ""
        try:
            if mt == "sticker":
                # #195：动图 .tgs / 视频 .webm 本体不是位图——识图用旁落的静态缩略图；
                # 没有缩略图就跳过（不再把 gzip/webm 字节喂给 VLM 白失败一轮）。
                from src.integrations.protocol_bridge import sticker_thumb_path
                still = sticker_thumb_path(local)
                if still:
                    desc = await _describe_image(still, cfg)
                else:
                    logger.debug("[media_enrich] 贴纸无静态缩略图，跳过识图 ref=%s", media_ref)
            elif mt in _IMAGE_KINDS:
                desc = await _describe_image(local, cfg)
            elif mt in _VOICE_KINDS:
                desc = await _transcribe_voice(local, cfg, voice_transcriber)
            elif mt in _VIDEO_KINDS:
                desc = await _understand_video(local, cfg, voice_transcriber)
        except Exception:
            logger.debug("[media_enrich] 识别失败 kind=%s", mt, exc_info=True)
            desc = ""

        desc = (desc or "").strip()
        if desc and mt in (_IMAGE_KINDS | _VIDEO_KINDS):
            # #143 C-补：描述落库前压单行——多行续行会逃过语言证据剥离
            desc = flatten_desc_line(desc)
        if desc and mt in (_IMAGE_KINDS | _VIDEO_KINDS) and desc_looks_garbled(desc):
            # 闸门：VLM 违抗「不要逐字抄零散文字」时兜底——展示/AI/翻译拿到的是诚实提示
            logger.info("[media_enrich] 识别产出判为碎片文字汤（%s，%d 字），已替换为提示",
                        mt, len(desc))
            _record_enrich(mt, cfg, understood=False, reason="garbled")
            desc = GARBLED_DESC_NOTE
        else:
            _record_enrich(mt, cfg, understood=bool(desc),
                           desc_type=(parse_desc_type(desc)[0] if desc else ""))
        if not desc:
            return (cap or media_placeholder(mt)), ""

        if mt in _VIDEO_KINDS:
            text = f"{cap}\n[视频内容] {desc}" if cap else f"[视频内容] {desc}"
        elif mt == "sticker":
            # #143（0902 工单）：贴纸不再冒充 [图片内容]——前缀就是下游（历史行
            # 重解析 _match_media_prefix / ai_client 媒体块 kind）的贴纸性载体，
            # 丢了它，贴纸在后续轮次会被当真实照片评论（「贴纸里的猫」实锤）。
            text = f"{cap}\n[贴纸内容] {desc}" if cap else f"[贴纸内容] {desc}"
        elif mt in _IMAGE_KINDS:
            text = f"{cap}\n[图片内容] {desc}" if cap else f"[图片内容] {desc}"
        elif mt in _VOICE_KINDS:
            # 语音转写即"对方说的话"，直接作为待回复正文（有 caption 少见，拼上）
            text = f"{cap}\n{desc}" if cap else desc
        else:
            text = cap or desc
        return text, desc
    finally:
        if tmp_download:
            try:
                os.unlink(tmp_download)
            except Exception:
                pass


#: 我方（坐席手动 / 手机端）发出的图片在上下文里的标签——与 ``persona_reply.normalize_history``
#: / i18n ``inbox.ms.media_out_label`` 同字面；后接配文与 ``[图片内容] 描述``。
OUT_IMAGE_LABEL = "[我方发出的图片]"


def outbound_desc_marker(media_type: str) -> str:
    """我方发出媒体的描述标记：图片 ``[图片内容]`` / 视频 ``[视频内容]``（与入站行同字面，
    下游剥离器 / 历史重解析同一口径）。其它类型 → ""（不回写）。"""
    mt = str(media_type or "").strip().lower()
    if mt in _VIDEO_KINDS:
        return "[视频内容]"
    if mt in _IMAGE_KINDS and mt != "sticker":
        return "[图片内容]"
    return ""


def _outbound_video_desc_enabled(cfg: Dict[str, Any]) -> bool:
    """``inbox.handoff_memory.outbound_video_desc``（默认开）：出站视频抽帧+音轨识别成本
    高于图片，手动发视频又极少——给个能关的口子。"""
    try:
        hm = ((cfg.get("inbox") or {}).get("handoff_memory") or {})
        return bool(hm.get("outbound_video_desc", True))
    except Exception:
        return True


async def describe_outbound_media(
    *, media_type: str, media_ref: str, config: Optional[Dict[str, Any]] = None,
    local_path: str = "",
) -> str:
    """识别**我方发出**的图片 / 视频（坐席手动发图 → 切回全自动后 AI 得知道自己"发过什么"）。

    复用入站识别链（同 VLM / 同缓存 / 同碎片文字闸；视频走 ``understand_video_file``
    抽关键帧 + 音轨 ASR）。贴纸 / 语音出站不识别（语音正文就是念的字）。返回单行描述；
    关/无后端/失败 → ""。全程软失败，绝不抛。``local_path`` 非空时直接用（发送路由手里
    就有落盘路径），否则按 ``media_ref`` 解析。
    """
    cfg = config or {}
    mt = str(media_type or "").strip().lower()
    is_video = mt in _VIDEO_KINDS
    if not is_video and (mt not in _IMAGE_KINDS or mt == "sticker"):
        return ""
    if is_video and not _outbound_video_desc_enabled(cfg):
        return ""
    try:
        local = str(local_path or "").strip()
        if not (local and os.path.isfile(local)):
            local = _resolve_local_path(media_ref) or ""
        if not local:
            return ""
        if is_video:
            desc = (await _understand_video(local, cfg, lazy_voice_transcriber(cfg)) or "").strip()
        else:
            desc = (await _describe_image(local, cfg) or "").strip()
        if not desc:
            return ""
        desc = flatten_desc_line(desc)
        if desc_looks_garbled(desc):
            return ""
        return desc[:600]
    except Exception:
        logger.debug("[media_enrich] 出站识别失败 kind=%s ref=%s", mt, media_ref, exc_info=True)
        return ""


async def _maybe_fetch_remote(
    media_ref: str, media_type: str, cfg: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """http(s) media_ref → 临时本地路径（调用方负责删除）。未开远程下载 / 非 URL → (None, None)。"""
    ref = str(media_ref or "").strip()
    if not ref.lower().startswith(("http://", "https://")):
        return None, None
    rf = ((cfg.get("media") or {}).get("remote_fetch") or {})
    if not rf.get("enabled"):
        return None, None
    mt = str(media_type or "").strip().lower()
    kind = "image" if mt in _IMAGE_KINDS else "audio"
    try:
        from src.inbox.media_fetch import fetch_remote_media
        path, reason = await fetch_remote_media(
            ref,
            kind=kind,
            max_bytes=int(rf.get("max_mb", 10) or 10) * 1024 * 1024,
            timeout_sec=float(rf.get("timeout_sec", 8) or 8),
            allow_domains=list(rf.get("allow_domains") or []),
        )
        if path:
            return path, path
        logger.debug("[media_enrich] remote_fetch 未取到文件 reason=%s", reason)
    except Exception:
        logger.debug("[media_enrich] remote_fetch 异常", exc_info=True)
    return None, None


__all__ = [
    "OUT_IMAGE_LABEL",
    "blind_image_assertion",
    "clear_desc_inflight",
    "describe_outbound_media",
    "enrich_inbound_media_text",
    "honest_image_ask",
    "is_placeholder_only",
    "lazy_voice_transcriber",
    "media_placeholder",
    "media_wait_sec_from_cfg",
    "try_mark_desc_inflight",
]
