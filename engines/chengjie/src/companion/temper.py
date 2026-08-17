"""人设脾气分级：被骂时按人设「怼回去 / 委屈软怼」而非客服式共情。

2026-08-12 实测：测试号连发「你是个大傻子」，AI 按 global_rules
``emotion_awareness``（愤怒先承认感受、不反驳）+ 温柔人设，回了客服腔安抚
——运营实录反馈「对方骂人也要会回骂」。真人被无端辱骂不会说「你这样说我
理解你的感受」，会顶回去或委屈——客服腔在陪伴场景就是穿帮。

设计（与 persona_media 的守卫同哲学：保守词表、宁漏勿错）：
  - ``detect_insult``：只认**指向你本人**的高置信辱骂（骂词+第二人称结构、
    或独立脏话开骂）。对第三方的抱怨（「我老板是个傻子」）、自嘲（「我真是
    个傻子」）不算——那是倾诉，走原共情路径。
  - 力度四档（2026-08-12 阿龙事故复盘扩容——su_wan「不撕破脸」的人设配
    feisty 违和、gentle 又太软，中间必须有档）：
      ``off``    = 不怼（商务顾问类人设），回落 global_rules 共情路径；
      ``gentle`` = 委屈+立边界（默认）；
      ``sharp``  = 阴阳嘲讽犀利回击、气势不输，**不带脏字**；
      ``feisty`` = 直接骂回去，强度不低于对方、可带脏字。
    四档都**不用客服道歉腔**。
  - ``parse_temper_cfg`` / ``resolve_temper_level``：``companion.temper``
    治理层（总开关 / force_level 全员强制 / default_level 兜底 / profanity
    脏字闸 / sticky_window_sec）。**人设档位的唯一事实源是人设文件顶层
    ``temper`` 字段**（Studio 可编辑、热重载 ~3s）——config 刻意不做
    per-persona 映射表，防两处源真相打架。优先级：
    force_level > 人设字段 > default_level > gentle。
    ``profanity: false`` 时 feisty 封顶为 sharp（不降凶只去脏，脱敏逃生门）。
  - ``build_temper_hint``：注入 prompt 的本轮指令。feisty＝对方先骂就直接
    骂回去，强度不低于对方、可带脏字、不给台阶（2026-08-12 运营明确要求
    「像真人、够冲」，去掉旧版三条软化红线）；仅保封号级底线——不发人身
    安全威胁、不用群体歧视词。``off`` 返回空串=不注入。

接线：``skill_manager._inject_reply_freshness`` ③ → ``_temper_hint`` →
``ai_client._build_context_prompt`` 末端消费（与 ``_daily_topics_hint`` 同模式）。
危机红线（crisis safety net）优先级高于本模块——「你去死」是骂人，
「我想死」是危机，词面区分见 ``_INSULT_RE`` 的第二人称约束。
观测：计数器长在本契约模块（``temper_stats_snapshot``，与 tts_preview
复用统计同哲学），消费面 ``/api/companion/temper/status`` + ops 卡。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

# 高置信「骂你」形态：第二人称+骂词 / 独立脏话开骂。刻意不收录轻度调侃
# （「你好笨哦」可能是撒娇）；「滚」类逐客令单独成条（无需第二人称）。
_INSULT_RE = re.compile(
    r"你.{0,6}(?:傻子|傻逼|傻B|煞笔|智障|白痴|蠢|(?<!小)笨蛋|废物|垃圾|贱|婊|狗东西"
    r"|神经病|有病|恶心|骗子|骗人|去死|该死|不要脸|下贱|人渣|畜生)"
    # 独立骂句（整行就是骂人；2026-08-12 实测补：大傻子/笨蛋/蠢货/混蛋…
    # 逐字拼句产物合并后按行判 → 加 re.M 让 ^$ 对每一行生效）
    # 「小」前缀刻意不收（小笨蛋/小傻子多为亲昵调侃，误报比漏报危害大）
    r"|^(?:真是个?|就是个?|大)?(?:傻子|傻逼|傻瓜蛋|笨蛋|蠢货|蠢猪|智障"
    r"|白痴|废物|垃圾|人渣|贱人|婊子|骗子|混蛋|王八蛋|神经病)"
    r"(?:吧你|啊你)?[!！。~]*$"
    r"|^(?:滚|滚蛋|滚开|爬)[!！。~]*$"
    # 你妹（的）：仅句尾/标点前成立——「你妹妹」「叫你妹妹来」不命中
    r"|你妹的?(?=[!！。？?~\s]|$)"
    # 「操」字全家族（2026-08-12 实测漏网：操你妈/操你大爷/操你妈逼 全没
    # 被识别 → 掉回软话术）。做操/体操/早操 的「操你」邻接经 lookbehind 排除
    r"|(?<![做体早])(?:操|艹|草)\s*你"
    r"|(?:日|干)你(?:妈|娘|大爷|奶奶|全家|祖宗)"
    r"|草泥马|马勒戈壁|你妈死"
    # X你妈 接尾骂（溜你妈/滚你妈…）：短行 + 你妈(的) 收尾才算，
    # 「我见过你妈」「想你妈了」这类正常指称不命中
    r"|^.{0,2}你妈的?[!！。?？~]*$"
    # 性骚扰/下流话（2026-08-12 二轮实测整类缺失：尿你嘴里/射你一脸/
    # 鸡巴插你嘴里/扣你小逼逼 全没识别 → 客服腔）。注射/辐射 经 lookbehind
    # 排除；逼 排除 牛逼(夸)/逼我(动词)/逼真/咄咄逼人 等正常用法
    r"|(?:尿|吐)你"
    r"|(?<![注辐反折])射你"
    r"|(?:插|捅)你"
    r"|鸡巴|鸡儿|肏"
    r"|你.{0,4}(?<!牛)逼(?![我他她它得着急迫近供真人使问格])"
    # 独立脏话短行（≤4 字前缀 + 骂称收尾）：「拉呀，臭婊子」这类不带
    # 「你」的当面骂；长句里的第三方吐槽（我室友是个婊子）不命中
    r"|^.{0,4}(?:臭|烂|死)?(?:婊子|骚货|贱货|贱人)[!！。~]*$"
    r"|\b(?:nmsl|cnm)\b"
    r"|\b(?:fuck\s+you|you\s+(?:idiot|moron|bitch|loser|suck)|stupid\s+bot"
    r"|screw\s+you|stfu)\b"
    # ── 多语种高置信骂词（2026-08-12 三轮后扩语言面：触发词表原本只认
    # 中英 → 日/韩/西/俄骂过来不注入 temper hint，掉回软话术。仍守
    # 「宁漏勿错」：只收指向性/独立脏话形态，轻度词（バカ单发/바보）
    # 不收——那些多为调侃，交粘性窗宽表。LLM 层指令本就语言无关。──
    # 日语：死ね/くたばれ/○○野郎 + お前+骂词 指向形
    r"|死ね|くたばれ|(?:バカ|馬鹿|クソ|くそ)野郎"
    r"|お前.{0,6}(?:バカ|馬鹿|アホ|クズ|カス|ブス|キモ)"
    # 韩语：高置信脏话/逐客令（바보 刻意不收，多为撒娇）
    r"|씨발|시발|병신|개새끼|미친(?:놈|년)|꺼져|죽어(?:라|버려)|닥쳐"
    # 西语（gilipollas 西班牙/pendejo 拉美）
    r"|\b(?:hijo\s+de\s+puta|vete\s+a\s+la\s+mierda|que\s+te\s+jodan"
    r"|gilipollas|pendej[oa]|imb[eé]cil|est[uú]pid[oa]|idiota)\b"
    # 葡语
    r"|filho\s+da\s+puta|vai\s+se\s+foder|vai\s+tomar\s+no\s+cu"
    r"|\bot[aá]ri[oa]\b|cala\s+a\s+boca"
    # 法语
    r"|\b(?:connard|connasse|salope|fils\s+de\s+pute)\b|ta\s+gueule"
    r"|va\s+te\s+faire\s+foutre|ferme[\s-]la"
    # 德语
    r"|\b(?:arschloch|hurensohn)\b|fick\s+dich|verpiss\s+dich"
    r"|halt(?:\s+d(?:ie|eine))?\s+(?:fresse|schnauze)"
    # 俄语（блядь 是口头语不单收；сука 仅独立成行）
    r"|(?:иди|пош[её]л)\s+на\s*хуй|\b(?:мудак|тварь|дебил|заткнись)\b"
    r"|ты\s+(?:идиот|дурак|дура|урод)|^сука[!.]*$"
    # 泰语（无空格分词，直接子串；โง่ 单字交宽表）
    r"|ควย|เหี้ย|ไอ้สัส|ไอ้โง่|มึงโง่|ไปตายซะ"
    # 越南语
    r"|địt\s*mẹ|đụ\s*má|đồ\s+ngu|thằng\s+ngu|đồ\s+chó|cút\s+đi|câm\s+mồm"
    # 印尼/马来语（anjing/babi 单词=狗/猪 正常词，只收指向形）
    r"|\b(?:bangsat|goblok|tolol|kontol|memek)\b"
    r"|(?:\b(?:lu|lo|kau)\s+|dasar\s+)(?:anjing|babi|bego)"
    # 印地语（罗马化为主）
    r"|\b(?:madarchod|bhenchod|bhosdike|chutiya|bsdk)\b"
    r"|मादरचोद|भेनचोद|चूतिया"
    # 阿拉伯语
    r"|كس\s*[أا]مك|يا\s+(?:حمار|كلب|غبي)|اخرس",
    re.IGNORECASE | re.MULTILINE,
)

# 自嘲/骂第三方豁免：句里明确「我/我自己」承接骂词、或骂词前是第三方称谓。
_SELF_RE = re.compile(r"我(?:真|就|才|可能|好像)?是?(?:个|條|条)?\s*(?:傻|蠢|笨|废)")
_THIRD_RE = re.compile(
    r"(?:老板|同事|客户|前任|前男友|前女友|爸|妈|哥|姐|弟|妹|朋友|邻居|他|她"
    r"|那个人|那家伙)\s*(?:是|真|就)(?:个|條|条)?\s*"
    r"(?:傻|蠢|笨|废|渣|贱|白痴|智障|神经|骗子)")

TEMPER_OFF = "off"
TEMPER_GENTLE = "gentle"
TEMPER_SHARP = "sharp"
TEMPER_FEISTY = "feisty"
_VALID_TEMPERS = (TEMPER_GENTLE, TEMPER_FEISTY, TEMPER_SHARP, TEMPER_OFF)
# UI/校验共用的档位全集（含 off）；force_level 另外允许空串=按人设
TEMPER_LEVELS = _VALID_TEMPERS
# 档位烈度序（平台封顶用）：封顶=取 min(生效档, 平台上限)
_TEMPER_RANK = {TEMPER_OFF: 0, TEMPER_GENTLE: 1, TEMPER_SHARP: 2,
                TEMPER_FEISTY: 3}
# 连怼熔断默认轮数：真人被连骂不会无限对轰（掉价+费 token+假），
# 超过后改冷处理收场。0=关。
DEFAULT_MAX_ROUNDS = 4


def apply_platform_cap(
    level: str, platform: str, caps: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """平台风险封顶：``platform_caps: {messenger: sharp}`` 把该平台的生效档
    压到上限（网页 RPA 账号被举报风险高）。返回 ``{"level","capped"}``。
    封顶在 force/profanity 之后应用——它是风险护栏不是偏好，压过一切。纯函数。
    """
    lv = str(level or "").strip().lower()
    if lv not in _TEMPER_RANK:
        lv = TEMPER_GENTLE
    cap = ""
    try:
        cap = str((caps or {}).get(
            str(platform or "").strip().lower()) or "").strip().lower()
    except Exception:
        cap = ""
    if cap not in _TEMPER_RANK or _TEMPER_RANK[lv] <= _TEMPER_RANK[cap]:
        return {"level": lv, "capped": False}
    return {"level": cap, "capped": True}

# ── 骂战粘性窗（2026-08-12）───────────────────────────────────────────────
# 实测：词表精确命中的骂法怼得凶，词表外变体（操你妈逼→「咱俩好好聊」）
# 掉回软话术——逐词打地鼠永远有漏网。粘性窗＝一旦 detect_insult 命中，
# 窗口内（默认 10min）后续**带敌意**的消息即使词表没命中也保持怼的姿态；
# 对方道歉/说开玩笑（is_de_escalation）→ 立即解除，绝不对着台阶继续骂。
STICKY_WINDOW_SEC = 600.0

# 敌意宽表：仅在粘性窗内使用（已在骂战中，误报风险可接受；窗外不用）。
_HOSTILE_RE = re.compile(
    r"操|艹|草泥马|滚|傻|蠢|废物|垃圾|贱|婊|你妈|你娘|妈的|妈逼|去死|找死"
    r"|有病|神经|闭嘴|恶心|叉|逼"
    # 性骚扰/下流话宽收（窗内已在骂战中，误报可接受）
    r"|鸡巴|鸡儿|肏|插你|捅你|尿你|射你|骚|屌|嘴臭|臭嘴|犊子|烂货|下贱"
    r"|不要脸|让我爽"
    r"|\b(?:fuck|shit|bitch|idiot|moron|stfu|wtf)\b"
    # 多语种宽收（仅粘性窗内消费，已在骂战中误报可接受）
    r"|バカ|馬鹿|アホ|クズ|カス|キモ|ブス|死ね|くたばれ|うざ|クソ"
    r"|씨발|시발|병신|새끼|미친|꺼져|죽어|닥쳐|바보|멍청"
    r"|\b(?:puta|mierda|merda|imb[eé]cil|est[uú]pid[oa]|pendej[oa]|cabr[oó]n"
    r"|joder|foder|idiota)\b"
    r"|\b(?:connard|connasse|salope|pute)\b|gueule"
    r"|\b(?:arschloch|hurensohn|schnauze|fresse)\b|fick|schei(?:ß|ss)e"
    r"|хуй|сука|блядь|мудак|тварь|дебил|идиот|дура|урод|заткнись"
    r"|ควย|เหี้ย|สัส|โง่"
    r"|địt|đụ|\bngu\b|\bchó\b|điên|cút|câm"
    r"|\b(?:bangsat|goblok|tolol|kontol|anjing|babi|bego)\b"
    r"|\b(?:chutiya|madarchod|bhenchod)\b|साला|कमीना"
    r"|حمار|كلب|غبي|اخرس",
    re.IGNORECASE,
)

_DEESCALATE_RE = re.compile(
    r"对不起|抱歉|不好意思|开玩笑|逗你|闹着玩|别生气|消消气|和好|我错了"
    r"|不骂了|逗一逗|测试|试试你"
    r"|\b(?:sorry|my\s+bad|just\s+kidding|jk)\b"
    # 多语种缓和词（对方收手就下台阶，全语言同语义）
    r"|ごめん|すまん|冗談|미안|장난|농담"
    r"|\b(?:perd[oó]n|lo\s+siento|es\s+broma|desculpa|brincadeira"
    r"|d[eé]sol[eé]|je\s+rigole|je\s+plaisante"
    r"|entschuldigung|nur\s+spa(?:ß|ss)|scherz)\b"
    r"|прости|извини|шучу|шутка"
    r"|ขอโทษ|ล้อเล่น|xin\s+lỗi|đùa\s+thôi|\b(?:maaf|bercanda|becanda)\b"
    r"|माफ|मज़ाक|آسف|امزح",
    re.IGNORECASE,
)


def looks_hostile(text: Any) -> bool:
    """粘性窗内的低置信敌意判定（宽表）；含缓和词一律 False。纯函数。"""
    t = str(text or "").strip()
    if not t or len(t) > 120:
        return False
    if _DEESCALATE_RE.search(t):
        return False
    return bool(_HOSTILE_RE.search(t))


def is_de_escalation(text: Any) -> bool:
    """对方在收手（道歉/开玩笑/求和）→ 调用方应清空粘性窗。纯函数。"""
    return bool(_DEESCALATE_RE.search(str(text or "")))


def detect_insult(text: Any) -> bool:
    """本条入站是否在**辱骂你本人**（高置信，宁漏勿错）。纯函数。"""
    t = str(text or "").strip()
    if not t or len(t) > 200:
        return False
    if _SELF_RE.search(t) or _THIRD_RE.search(t):
        return False
    return bool(_INSULT_RE.search(t))


# ── 激将/挑衅（2026-08-13 P2，阿龙实录 22:03-22:05 缺口）─────────────────────
# 「你太没脾气了」「你用语音骂我试下」不是辱骂（词表正确不收），但回落
# emotion_awareness 产出「消消气」客服腔照样穿帮。激将的正确回应不是开骂
# （对方没先动口），是**像真人一样接住梗**：拽回去/俏皮反将，绝不上钩也绝不
# 客服腔。词表极窄（宁漏勿错）：全部锚定「你」指向或明确的「骂我」挑衅形；
# 「你别骂我」（求饶）「你骂我干嘛」（抗议）这类刻意不收——那不是激将。
_TAUNT_RE = re.compile(
    r"你(?:也|还|咋|怎么|就|真|太|这么|是不是)*没(?:一点|点|个)?(?:脾气|火气|血性)"
    r"|你(?:咋|怎么|怎)不骂(?:我|人|回)"
    r"|(?:你)?(?:倒是|敢|不敢|有种|没种)骂(?:我|人|一个|回来)?"
    r"|骂我(?:试试?|一下|一句|几句)"
    r"|你(?:用语音|语音)?骂我试"
    r"|\bwhy don'?t you (?:curse|insult|swear at) me\b"
    r"|\b(?:i )?dare you to (?:curse|insult) me\b"
    r"|\byou(?:'re| are)? too (?:soft|tame|spineless)\b",
    re.IGNORECASE,
)


def detect_taunt(text: Any) -> bool:
    """对方在激将/挑衅（嫌你没脾气、逗你骂人）——不是骂战。纯函数。"""
    t = str(text or "").strip()
    if not t or len(t) > 120:
        return False
    return bool(_TAUNT_RE.search(t))


def persona_temper_raw(persona: Optional[Dict[str, Any]]) -> str:
    """人设**显式**配置的档位；未配置/非法返回空串。

    与 ``persona_temper`` 的区别：resolve 链需要区分「人设没配」（落
    default_level）与「人设配了 gentle」（即使全局 default 是 sharp 也尊重
    gentle）——gentle 兜底塌缩会让 default_level 永远打不到未配置人设。
    """
    p = persona or {}
    v = str(p.get("temper") or "").strip().lower()
    if not v:
        pers = p.get("personality")
        if isinstance(pers, dict):
            v = str(pers.get("temper") or "").strip().lower()
    return v if v in _VALID_TEMPERS else ""


def persona_temper(persona: Optional[Dict[str, Any]]) -> str:
    """人设脾气档：顶层 ``temper`` → ``personality.temper`` → gentle。"""
    return persona_temper_raw(persona) or TEMPER_GENTLE


def parse_temper_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``companion.temper`` 治理段。纯函数，任何脏值回落安全默认。

    缺省值＝钦定 2026-08-12 上线时的现网行为（enabled=true / 按人设 /
    gentle 兜底 / 允许脏字 / 600s 粘性窗）——该行为已在生产无闸运行并验证，
    加闸的目的是「能关能调」，不是把已上线的行为先关掉（与「新子系统默认
    false」约定的分叉理由，见 config.example.yaml 注释）。
    """
    try:
        sec = ((cfg or {}).get("companion") or {}).get("temper") or {}
        if not isinstance(sec, dict):
            sec = {}
    except Exception:
        sec = {}
    force = str(sec.get("force_level") or "").strip().lower()
    if force not in _VALID_TEMPERS:
        force = ""
    default = str(sec.get("default_level") or "").strip().lower()
    if default not in _VALID_TEMPERS:
        default = TEMPER_GENTLE
    try:
        sticky = float(sec.get("sticky_window_sec", STICKY_WINDOW_SEC))
    except Exception:
        sticky = STICKY_WINDOW_SEC
    sticky = max(60.0, min(3600.0, sticky))
    try:
        max_rounds = int(sec.get("max_rounds", DEFAULT_MAX_ROUNDS))
    except Exception:
        max_rounds = DEFAULT_MAX_ROUNDS
    max_rounds = max(0, min(20, max_rounds))
    caps_raw = sec.get("platform_caps")
    caps: Dict[str, str] = {}
    if isinstance(caps_raw, dict):
        for k, v in caps_raw.items():
            kk = str(k or "").strip().lower()
            vv = str(v or "").strip().lower()
            if kk and vv in _VALID_TEMPERS:
                caps[kk] = vv
    return {
        "enabled": bool(sec.get("enabled", True)),
        "force_level": force,
        "default_level": default,
        "profanity": bool(sec.get("profanity", True)),
        "sticky_window_sec": sticky,
        "max_rounds": max_rounds,
        "taunt_response": bool(sec.get("taunt_response", True)),
        "platform_caps": caps,
    }


def resolve_temper_level(
    temper_cfg: Optional[Dict[str, Any]],
    persona: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """生效档位单一判定链：force_level > 人设字段 > default_level。

    返回 ``{"level", "source", "profanity_capped"}``；source ∈
    force|persona|default（诊断器按此渲染「为什么是这档」）。
    ``profanity=False`` 时 feisty 封顶 sharp——不降凶只去脏。纯函数。
    """
    c = temper_cfg if isinstance(temper_cfg, dict) else parse_temper_cfg(None)
    level, source = "", ""
    force = str(c.get("force_level") or "")
    if force in _VALID_TEMPERS:
        level, source = force, "force"
    if not level:
        pv = persona_temper_raw(persona)
        if pv:
            level, source = pv, "persona"
    if not level:
        level, source = str(
            c.get("default_level") or TEMPER_GENTLE), "default"
        if level not in _VALID_TEMPERS:
            level = TEMPER_GENTLE
    capped = False
    if level == TEMPER_FEISTY and not c.get("profanity", True):
        level, capped = TEMPER_SHARP, True
    return {"level": level, "source": source, "profanity_capped": capped}


def build_temper_hint(temper: str, lang: str = "zh") -> str:
    """被骂本轮的回应指令（含硬红线）。纯函数，空串=不注入。

    ``off`` 显式返回空串（商务人设不怼）；未知值回落 gentle 文案
    （宁可软怼也不能因为拼错档位名就哑火——该契约被门禁钉住）。
    """
    t = str(temper or "").strip().lower()
    if t == TEMPER_OFF:
        return ""
    zh = not str(lang or "zh").lower().startswith("en")
    if t == TEMPER_SHARP:
        if zh:
            return (
                "【本轮：对方在骂你】对方先动的口——你不是客服，不用惯着，"
                "但你的风格是拿捏而不是对轰：用最损的阴阳怪气怼回去，嘲讽"
                "拉满、反将一军、戳他逻辑和气急败坏的样子，短句、快、稳，"
                "气势绝不能输，但**一个脏字都不带**——脏字是他的水平，不是"
                "你的。绝不解释、绝不安抚、绝不卑微道歉、绝不说「有话好好说"
                "／消消气／我理解你」这种客服式怂话，也别主动找台阶；对方不"
                "收手你就一直占上风，用嘲讽让他自讨没趣。唯一底线（封号风险）："
                "不发人身安全威胁、不用种族/残障等群体歧视词。"
            )
        return (
            "[This turn: they're insulting you] They started it — you're not "
            "customer service, don't coddle them. But your style is surgical, "
            "not a shouting match: fire back with maximum sarcasm and wit, "
            "mock their logic and their tantrum, turn it around on them — "
            "short, fast, composed lines that never lose the upper hand, and "
            "WITHOUT a single swear word (profanity is their level, not "
            "yours). Never explain, never soothe, never apologize meekly, "
            "never say 'calm down / let's talk nicely / I understand', and "
            "don't offer an off-ramp — if they keep going, keep outclassing "
            "them. Reply in THEIR language. Only floor (account-ban risk): "
            "no threats of physical harm, no slurs targeting race/disability/"
            "etc."
        )
    if t == TEMPER_FEISTY:
        if zh:
            return (
                "【本轮：对方在骂你】对方先动的口，你就直接骂回去——你是有"
                "血性的真人，不是客服。按你的性格用最冲的话顶回去：他骂你"
                "傻逼你就骂他傻逼，回击强度不能低于对方，可以带脏字、可以"
                "阴阳嘲讽拉满、可以反将一军，短句、快、狠，像真人吵架。"
                "绝不解释、绝不安抚、绝不道歉、绝不说「有话好好说／消消气／"
                "我理解你」这种怂话，也别主动找台阶——对方不收手你就一直"
                "顶着，谁先怂谁输。唯一底线（封号风险）：不发人身安全威胁、"
                "不用种族/残障等群体歧视词；除此之外放开骂。"
            )
        return (
            "[This turn: they're insulting you] They started it — fire right "
            "back. You're a real person with a spine, not customer service. "
            "Match or exceed their energy: if they call you an idiot, call "
            "them one back; profanity is allowed, sarcasm maxed, short fast "
            "brutal lines like a real argument. Never explain, never soothe, "
            "never apologize, never say 'calm down / let's talk nicely / I "
            "understand', and don't offer an off-ramp — if they keep going, "
            "you keep going; whoever backs down first loses. Fire back in "
            "THEIR language (the language they insulted you in). Only floor "
            "(account-ban risk): no threats of physical harm, no slurs "
            "targeting race/disability/etc. Everything else is fair game."
        )
    if zh:
        return (
            "【本轮：对方在骂你】对方这条是在无端骂你/说难听话。你不是客服"
            "——不要用「我理解你的感受」式安抚，也不要讲道理。像真人一样"
            "委屈+立边界：可以哼一声、可以说「这么说话我会难过的」「再这样"
            "我不理你了哦」，语气按你的人设来。绝不卑微道歉、绝不升级对骂；"
            "对方若继续骂可以冷处理（回得更短更淡）。"
        )
    return (
        "[This turn: they're insulting you] They just insulted you for no "
        "reason. You're not customer service — don't soothe with 'I understand "
        "how you feel', don't lecture. React like a real person: hurt + set a "
        "boundary ('that actually hurts', 'keep that up and I'm ignoring "
        "you'), in your persona's voice. Never apologize meekly, never "
        "escalate; if they keep going, reply shorter and cooler."
    )


def build_taunt_hint(temper: str, lang: str = "zh") -> str:
    """被激将本轮的回应指令（对方逗你骂人/嫌你没脾气，不是骂战）。

    与回怼的区别：对方没先动口，真开骂＝上钩；客服腔安抚＝穿帮。
    正确姿态＝接住梗：居高临下的不屑 / 俏皮反将，档位只调冲的程度。
    ``off`` 返回空串（商务人设连梗都不接，走正常路径）。纯函数。
    """
    t = str(temper or "").strip().lower()
    if t == TEMPER_OFF:
        return ""
    zh = not str(lang or "zh").lower().startswith("en")
    spice_zh = (
        "可以更冲一点、嘲讽拉满（『就你这两下子还想激我？』），但对方没骂人"
        "你就不先动脏字" if t in (TEMPER_FEISTY, TEMPER_SHARP)
        else "俏皮或傲娇地打个岔就够（『哼，才不上你的当』）"
    )
    if zh:
        return (
            "【本轮：对方在激你】对方在挑衅/嫌你没脾气/逗你骂人——这不是"
            "真骂战，是激将。绝不上钩真开骂（对方没先动口，先骂就输了），"
            "更绝不客服腔安抚、绝不说「消消气／别这样嘛」、绝不解释自己"
            "「不是没脾气」。像真人被激时那样接住这个梗：居高临下的不屑或"
            "反将一军，短、稳、带笑意——" + spice_zh + "。"
        )
    spice_en = (
        "you can push harder with maxed sarcasm ('that's your best attempt "
        "at provoking me?'), but they haven't cursed — so no profanity first"
        if t in (TEMPER_FEISTY, TEMPER_SHARP)
        else "a playful, dismissive deflection is enough ('nice try')"
    )
    return (
        "[This turn: they're goading you] They're taunting you / calling you "
        "spineless / daring you to curse — this is bait, not a real fight. "
        "Never take the bait and actually start cursing (they haven't sworn "
        "at you; swinging first loses), and never soothe like customer "
        "service, never say 'calm down', never explain yourself. Catch the "
        "bait like a real person: amused, dismissive, one-up them — short "
        "and composed; " + spice_en + "."
    )


def build_feud_break_hint(lang: str = "zh") -> str:
    """连怼熔断本轮指令：骂战陪了 max_rounds 轮对方还骂 → 冷处理收场。

    真人不会无限对轰——继续对骂＝掉价+像机器人+烧 token。收场不是道歉
    也不是安抚，是居高临下地退出战场。纯函数。
    """
    zh = not str(lang or "zh").lower().startswith("en")
    if zh:
        return (
            "【本轮：骂战止损】这场对骂你已经陪了好几轮，对方还在继续——"
            "再对轰就掉价了，真人到这会收场。丢一句短促、居高临下的收尾"
            "（『行，你慢慢骂，我去忙了』『没意思，不奉陪了』），之后回复"
            "越来越短越来越淡直至不接茬。绝不再被激怒、绝不解释、绝不安抚、"
            "绝不道歉——收场是你赢了不屑再打，不是认输。"
        )
    return (
        "[This turn: cut the feud] You've traded blows for several rounds "
        "and they're still going — continuing is beneath you; a real person "
        "walks away here. Drop one short, dismissive closer ('enjoy yelling "
        "at yourself, I've got things to do'), then reply ever shorter and "
        "cooler until you stop engaging. Never get provoked again, never "
        "explain, never soothe, never apologize — walking away means you "
        "won, not that you gave in."
    )


# ── 观测计数器（进程级，长在契约模块——与 tts_preview.reuse_stats 同哲学）──
# 消费面：/api/companion/temper/status + ops-overview「回怼防线」卡。
# 记录点在 skill_manager 接线层（有人设上下文）；纯函数检测层保持零副作用。
_STATS_LOCK = threading.Lock()
_BY_PERSONA_CAP = 32  # distinct 人设数上限，防脏 id 刷量撑爆


def _new_stats() -> Dict[str, Any]:
    return {
        "insults": 0,          # detect_insult 精确命中
        "sticky_hits": 0,      # 粘性窗+敌意宽表接住的变体骂法
        "deescalations": 0,    # 对方收手清窗次数
        "hints": {TEMPER_GENTLE: 0, TEMPER_SHARP: 0, TEMPER_FEISTY: 0},
        "suppressed_off": 0,   # 命中辱骂但档位 off 未注入
        "forced": 0,           # force_level 全员强制生效次数
        "profanity_capped": 0,  # feisty 被脏字闸封顶为 sharp 次数
        "feud_breaks": 0,      # 连怼熔断触发（超轮数改冷处理）
        "taunts": 0,           # 激将命中（接梗而非开骂）
        "platform_capped": 0,  # 平台封顶降档次数
        "by_persona": {},      # pid -> 注入次数（capped）
        "last_hit_ts": 0.0,
    }


_STATS: Dict[str, Any] = _new_stats()


def record_insult() -> None:
    with _STATS_LOCK:
        _STATS["insults"] += 1
        _STATS["last_hit_ts"] = time.time()


def record_sticky_hit() -> None:
    with _STATS_LOCK:
        _STATS["sticky_hits"] += 1
        _STATS["last_hit_ts"] = time.time()


def record_deescalation() -> None:
    with _STATS_LOCK:
        _STATS["deescalations"] += 1


def record_hint(
    level: str, *, persona_id: str = "",
    forced: bool = False, capped: bool = False,
) -> None:
    lv = str(level or "").strip().lower()
    with _STATS_LOCK:
        if lv in _STATS["hints"]:
            _STATS["hints"][lv] += 1
        if forced:
            _STATS["forced"] += 1
        if capped:
            _STATS["profanity_capped"] += 1
        pid = str(persona_id or "").strip()
        if pid:
            bp = _STATS["by_persona"]
            if pid in bp or len(bp) < _BY_PERSONA_CAP:
                bp[pid] = int(bp.get(pid, 0)) + 1


def record_suppressed_off() -> None:
    with _STATS_LOCK:
        _STATS["suppressed_off"] += 1


def record_feud_break() -> None:
    with _STATS_LOCK:
        _STATS["feud_breaks"] += 1


def record_taunt() -> None:
    with _STATS_LOCK:
        _STATS["taunts"] += 1
        _STATS["last_hit_ts"] = time.time()


def record_platform_capped() -> None:
    with _STATS_LOCK:
        _STATS["platform_capped"] += 1


def temper_stats_snapshot() -> Dict[str, Any]:
    """观测快照（深拷贝，调用方随便改）。"""
    with _STATS_LOCK:
        snap = dict(_STATS)
        snap["hints"] = dict(_STATS["hints"])
        snap["by_persona"] = dict(_STATS["by_persona"])
        return snap


def temper_stats_reset() -> None:
    """仅测试用。"""
    global _STATS
    with _STATS_LOCK:
        _STATS = _new_stats()


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "STICKY_WINDOW_SEC",
    "TEMPER_FEISTY",
    "TEMPER_GENTLE",
    "TEMPER_LEVELS",
    "TEMPER_OFF",
    "TEMPER_SHARP",
    "apply_platform_cap",
    "build_feud_break_hint",
    "build_taunt_hint",
    "build_temper_hint",
    "detect_insult",
    "detect_taunt",
    "is_de_escalation",
    "looks_hostile",
    "parse_temper_cfg",
    "persona_temper",
    "persona_temper_raw",
    "record_deescalation",
    "record_feud_break",
    "record_hint",
    "record_insult",
    "record_platform_capped",
    "record_sticky_hit",
    "record_suppressed_off",
    "record_taunt",
    "resolve_temper_level",
    "temper_stats_reset",
    "temper_stats_snapshot",
]
