# -*- coding: utf-8 -*-
"""口语化改写层 v1（2026-07-31《口语化说话方式_文本腔根治_三视角方案》P1 主刀）。

定位：LLM 回复 → 【本层】 → TTS。把「作文腔」合成段改写成「微信语音逐字稿」风格，
只动朗读文本（字幕/历史仍是干净稿，与 _spoken_polish 同哲学）。根因见方案文档：
云端 LLM 对口语指令按下限执行（07-27 实锤），出口注入只能贴句尾膏药，句子架构仍是
书面的——本层用本地小模型整段重写句子架构，成功则接管（跳过旧注入防double），
失败/超时/校验不过 → 返回 None 原句直通（宁不改不可断流、不可慢）。

四道保险：
  1. 热旗标 data/conv_colloquial.flag（2s TTL）：off/缺省=关；"on"=有指纹的角色全开；
     "角色A,角色B"=仅这些角色（灰度）。环境变量 CONV_COLLOQUIAL=1 等效 "on"（旗标优先）。
  2. 事实锁 _fact_lock_ok：数字/英文串必须原样保留；不/没/别 计数不许减少（防语义翻转）；
     方括号标记（[呼吸]/【高兴】等）不丢不增；长度带 0.45~1.8×；改写器自我暴露词即拒。
  3. 超时铡刀：首段 CONV_COLLOQ_T1（默认 2.8s）/ 后续段 CONV_COLLOQ_T2（默认 5s）；
     长段(≥40 汉字)即便 first 也走 T2——整轮合成下首段=全文，窄超时会系统性误杀。
     超时立即直通；后续段与上一段合成重叠，感知零增量。
  4. 打断让路：调用方（conversation.run_turn）用 cancel 竞速包裹（八轮打断手术先例）。

后端：qwen14b-173（.173 ollama /v1/chat/completions，OpenAI 兼容；TTFT 实测 0.11~0.14s、
214~228 字/s → 40 字段 ≈0.3s，零 API 费）。CONV_COLLOQ_LLM/CONV_COLLOQ_MODEL 可换。

说话指纹：data/speech_prints.json（角色→习惯描述+口头禅池；mtime 热加载）。
同一份指纹供两处使用：① P0 系统提示词注入（prompt_block，逐角色稳定→KV 前缀友好）；
② 本层改写提示。few-shot 书面→口语对以今日 Mizuki 四轮盲听胜出稿为蓝本（主人定音）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).resolve().parent
_PRINTS_PATH = _BASE / "data" / "speech_prints.json"
_FLAG_PATH = _BASE / "data" / "conv_colloquial.flag"

_LLM_URL = os.environ.get("CONV_COLLOQ_LLM",
                          "http://192.168.0.173:11434/v1/chat/completions")
_LLM_MODEL = os.environ.get("CONV_COLLOQ_MODEL", "qwen14b-fallback")
# [2026-08-01] 默认抬高：冷启动/整轮合成下 1.5s 首段铡刀实测易误杀（改写成功变直通
# =听感作文腔）。环境变量仍可覆盖；每次调用热读（改 env/旗标免重启）。
_T_FIRST_DEFAULT = 2.8
_T_REST_DEFAULT = 5.0
_ENV_ON = os.environ.get("CONV_COLLOQUIAL", "0") == "1"
# 短句门槛：原 12 字（方案保 TTFA）把寒暄「早呢，最近咋样？」整段跳过——用户感知
# 最强的开口句反而不改。降至 6：更短的「嗯。」「好的。」仍直通。
_MIN_HAN = max(4, int(os.environ.get("CONV_COLLOQ_MIN_HAN", "6")))


def _timeouts() -> tuple:
    """热读超时：CONV_COLLOQ_T1/T2 环境变量，缺省 2.8/5.0。"""
    try:
        t1 = float(os.environ.get("CONV_COLLOQ_T1", str(_T_FIRST_DEFAULT)))
    except Exception:
        t1 = _T_FIRST_DEFAULT
    try:
        t2 = float(os.environ.get("CONV_COLLOQ_T2", str(_T_REST_DEFAULT)))
    except Exception:
        t2 = _T_REST_DEFAULT
    return max(0.5, t1), max(t1, t2)

logger = logging.getLogger("colloquial")

# ── 说话指纹（mtime 热加载）─────────────────────────────────────────
_prints_cache: dict = {"mtime": -1.0, "data": {}}


def _prints() -> dict:
    try:
        st = _PRINTS_PATH.stat()
    except Exception:
        return {}
    if st.st_mtime != _prints_cache["mtime"]:
        try:
            _prints_cache["data"] = json.loads(_PRINTS_PATH.read_text(encoding="utf-8"))
            _prints_cache["mtime"] = st.st_mtime
        except Exception as e:
            logger.warning(f"speech_prints.json 解析失败（沿用旧值）: {e}")
    return _prints_cache["data"] or {}


# ── 热旗标（2s TTL，与 conv_stream.flag 同款）───────────────────────
_flag_cache = {"t": 0.0, "v": None}


def _flag_state() -> object:
    """返回 False（关）/ True（全开）/ set(角色名)（灰度名单）。"""
    now = time.time()
    if now - _flag_cache["t"] > 2.0:
        v: object = None
        try:
            if _FLAG_PATH.is_file():
                raw = _FLAG_PATH.read_text(encoding="utf-8").strip()
                low = raw.lower()
                if low in ("", "off", "0", "false"):
                    v = False
                elif low in ("on", "1", "true", "all"):
                    v = True
                else:
                    v = {r.strip() for r in re.split(r"[,，;；\s]+", raw) if r.strip()}
        except Exception:
            v = None
        if v is None:
            v = _ENV_ON
        _flag_cache["v"] = v
        _flag_cache["t"] = now
    return _flag_cache["v"]


def role_enabled(role: str) -> bool:
    st = _flag_state()
    if st is False or not role:
        return False
    if role not in _prints():          # 无指纹的角色不开（文本×声学要配套，方案拍板#1）
        return False
    return True if st is True else (role in st)


# ── P0：说话指纹 → 系统提示词段（逐角色稳定，KV 前缀友好）────────────
def prompt_block(role: str) -> str:
    p = _prints().get(role) or {}
    pr = p.get("print", "")
    if not pr:
        return ""
    # guide 可按角色覆盖（P2 全角色铺开的教训：话少句短的硬汉/船长、播报定位的
    # 官方主播，套「碎句+想词」是反人设的——这类角色在 speech_prints.json 里自带 guide）。
    guide = p.get("guide") or (
        "写回复时照这个习惯说话：句子短碎、像微信语音的逐字稿，"
        "可以带想词（嗯…、诶…）、程度词说两遍、偶尔说错个小地方再改口；"
        "别写书面腔，别用书面连接词（然而/因此/首先/总之）。")
    out = "【说话指纹】" + pr + "\n" + guide
    ex = p.get("example", "")
    if ex:
        out += "\n你平时说话就是这样的（语感范文，别照抄内容）：" + ex
    return out


# ── 事实锁 ───────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")
_ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+")
_TAG_RE = re.compile(r"\[[^\[\]\n]{1,8}\]|【[^【】\n]{1,8}】")
# 段首/段尾情绪标签摘接 + 产物卫生：同源 emo_tag（2026-08-01 独立模块）。
# 模块缺失时退回本地正则，行为等价。
try:
    import emo_tag as _emo_tag
    _LEAD_TAG_RE = _emo_tag.LEAD_TAG_RE
    _TAIL_TAG_RE = _emo_tag.TAIL_TAG_RE
    _EMO_JUNK_RE = _emo_tag.TAG_RE
except Exception:
    _LEAD_TAG_RE = re.compile(r"^\s*(\[[^\[\]\n]{1,44}\]|【[^【】\n]{1,40}】)\s*")
    _TAIL_TAG_RE = re.compile(r"\s*(\[[^\[\]\n]{1,44}\]|【[^【】\n]{1,40}】)\s*$")
    _EMO_JUNK_RE = re.compile(
        r"[\[【（(｛{]{1,3}\s*[$＄]?\s*[｛{]?\s*情绪\s*[:：]"
        r"[^\]】）)｝}\n]{0,44}[\]】）)｝}]{1,3}")
_LEAK_RE = re.compile(r"改写|口语化|逐字稿|以下是|输出[:：]|原文")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_SKIP_RE = re.compile(r"https?://|www\.|@|[\w.]+@[\w.]+")

# 中文数目字 → 数值（口语渲染等价：LLM 把 30 说成「三十」是合法口语、不是事实漂移；
# 单测 3/6 被拒全因这个。按数值判等——「三十」≡30 放行、数值变了仍死拦）。
_CN_DIG = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_RUN_RE = re.compile(r"[零一二两三四五六七八九十百千万亿]+"
                        r"(?:点[零一二两三四五六七八九]+)?")


def _cn_num_val(s: str) -> Optional[str]:
    """「两千九百九十九」→"2999"，「三点五」→"3.5"。解析失败返回 None。"""
    try:
        frac = ""
        if "点" in s:
            s, tail = s.split("点", 1)
            frac = "".join(str(_CN_DIG[c]) for c in tail if c in _CN_DIG)
        total = section = num = 0
        for ch in s:
            if ch in _CN_DIG:
                num = _CN_DIG[ch]
            elif ch == "十":
                section += (num or 1) * 10; num = 0
            elif ch == "百":
                section += (num or 1) * 100; num = 0
            elif ch == "千":
                section += (num or 1) * 1000; num = 0
            elif ch in ("万", "亿"):
                total = (total + section + num) * (10000 if ch == "万" else 10 ** 8)
                section = num = 0
        v = total + section + num
        return f"{v}.{frac}" if frac else str(v)
    except Exception:
        return None


def _dst_num_values(dst: str) -> set:
    vals = set()
    for m in _CN_RUN_RE.finditer(dst):
        v = _cn_num_val(m.group(0))
        if v is not None:
            vals.add(v)
    return vals


# 否定词计数（E2E 首战教训：裸数「别」字被「特别→超」这种改写误伤——特别/区别的
# 别不是否定）。用视断言排掉常见非否定复合词，只数真否定；dst 计数不得少于 src。
_NEG_RES = (
    # 「多」不再整体排除（03:30 巡检：「次数也不多」是真否定，被旧名单错放）；
    # 「差不多」这一个成语改由 _NEG_NORM_SUBS 归一处理。
    ("不", re.compile(r"不(?!错|过|禁|由|妨)")),
    ("没", re.compile(r"没(?!关系)")),
    # 后视=复合词前缀（特别/区别…）；前视=复合词后缀（别致/别扭/别人/别的…
    # 2026-08-01 09:54 实锤「挺别致啊」被当劝阻拦截）。劝阻「别去/别慌」不受影响。
    ("别", re.compile(r"(?<!特)(?<!区)(?<!级)(?<!识)(?<!分)(?<!告)(?<!派)"
                      r"别(?!致|墅|扭|的|人|处|样|具|号|院|称|提多)")),
)

# [2026-07-31 全角色灰度首晚·生产实锤三类误伤] 计数前先做等价归一（src/dst 对称
# 应用，只影响计数不改文本）：这些「不/没」是疑问/连接/附和成分，不是否定——
#   ① A不A/A没A 问式：是不是/对不对/去没去（中间那个不算否定）
#   ② 方言问尾：「开到16度没得？」「去不？」（问号前的 没(得)/不 = 疑问助词）
#   ③ 要不就/要不然（=或者）；可不是嘛/可不嘛/可不咋地（附和成语，表肯定）
# 同晚真拦截「不想走→想走人」（语义反转）不受影响：普通「不X」原样计数。
_NEG_NORM_SUBS = (
    (re.compile(r"(.)不\1"), r"\1\1"),
    (re.compile(r"(.)没\1"), r"\1\1"),
    (re.compile(r"[没不]得?(?=[？?])"), ""),
    (re.compile(r"要不(?=[然就])"), "要"),
    (re.compile(r"可不(?=是?[嘛吗]|咋地)"), "可"),
    (re.compile(r"差不多"), "差多"),   # 成语整体非否定；裸「不多」是真否定要计数
)


def _neg_norm(t: str) -> str:
    for rex, rep in _NEG_NORM_SUBS:
        t = rex.sub(rep, t)
    return t


def _neg_dropped(src: str, dst: str) -> Optional[str]:
    src, dst = _neg_norm(src), _neg_norm(dst)
    # [2026-08-01 03:30 巡检实锤] 不/没 同池计总数：「没聊几回→聊天次数也不多」是
    # 等义换词（极性未变），分桶计数会误伤。真丢失（不想走→想走人）总数必减仍拦；
    # 「别」不进池——劝阻语气无等义替身，保持逐字严格。
    _bu, _mei = _NEG_RES[0][1], _NEG_RES[1][1]
    if (len(_bu.findall(dst)) + len(_mei.findall(dst))
            < len(_bu.findall(src)) + len(_mei.findall(src))):
        return "不/没"
    name, rex = _NEG_RES[2]
    if len(rex.findall(dst)) < len(rex.findall(src)):
        return name
    return None


# 问句保持（2026-07-31 归一化的补偿锁）：方言问尾的没/不放行后，「你记住没？→
# 记住了。」这种**问句改陈述**（问答方向反转）会失去否定计数的误打误撞拦截，
# 补一道显式锁：src 有问号 → dst 必须还有问号。
_Q_RE = re.compile(r"[？?]")


def _question_lost(src: str, dst: str) -> bool:
    return bool(_Q_RE.search(src)) and not _Q_RE.search(dst)


def _num_missing(src: str, dst: str) -> Optional[str]:
    """src 每个数字必须以阿拉伯原样或中文数目字等值出现在 dst；百分数还要求
    dst 里有 % 或「百分之/成」。返回第一个丢失的数字，全齐返回 None。"""
    dst_vals = None
    for m in set(_NUM_RE.findall(src)):
        base = m.rstrip("%")
        if m in dst:
            continue
        if dst_vals is None:
            dst_vals = _dst_num_values(dst)
        canon = base if "." in base else str(int(base))
        if base not in dst and canon not in dst_vals:
            return m
        if m.endswith("%") and "%" not in dst and "百分之" not in dst and "成" not in dst:
            return m
    return None


def _fact_lock_ok(src: str, dst: str) -> bool:
    if not dst or not dst.strip():
        return False
    sl, dl = len(src), len(dst)
    if dl > sl * 1.8 + 12 or dl < max(4, int(sl * 0.45)):
        return False
    if _num_missing(src, dst) is not None:
        return False
    for m in set(_ASCII_RE.findall(src)):
        if m.lower() not in dst.lower():
            return False
    if _neg_dropped(src, dst) is not None:
        return False
    if _question_lost(src, dst):
        return False
    src_tags = _TAG_RE.findall(src)
    for t in set(src_tags):
        if dst.count(t) != src_tags.count(t):
            return False
    for t in set(_TAG_RE.findall(dst)):
        if t not in src_tags:
            return False                     # 不许发明新标记
    if _LEAK_RE.search(dst):
        return False
    return True


# ── 改写提示（few-shot 蓝本 = 今日四轮盲听主人定音稿）────────────────
_FEWSHOT = (
    # 第一对 2026-07-31 主人「还可以更口语」后加强：53 分版 → 66 分版（探针口径）。
    # 范文即锚——模型模仿的就是范文浓度，范文分低产物就低。
    "书面：这家店的招牌是金枪鱼寿司，食材每天清晨从市场采购，非常新鲜，价格也不贵，值得一试。\n"
    "口语：他家招牌嘛，是那个…金枪鱼寿司。食材呢，都是每天一大早从市场拉回来的，"
    "特别特别新鲜，还不贵。真的诶，你一定得去试试。\n"
    "书面：我妈的店周末特别忙。我帮她切三文鱼的时候不小心把刀切出了一个缺口，"
    "她心疼那把刀，念叨了我一个晚上。\n"
    "口语：就我妈那店，周末是真的忙，真的。我就帮她切鱼嘛，切着切着……诶，那个刀，"
    "咔一下就豁了个口儿。完了她心疼那刀啊，念叨了我一晚上，我说妈，不至于，真不至于。\n"
    # 第三对：示范方括号标记原位保留（2026-07-31 生产拒绝实录：qwen14b 丢正文中部
    # [呼吸] → 事实锁拒绝。规则 3 已写明但小模型偶尔无视，用示范教比加粗规则管用）。
    "书面：等了三个月的演唱会门票终于抢到了。[呼吸]虽然座位在山顶位置，但是能去现场已经很满足了。\n"
    "口语：等了仨月的票诶，终于终于抢到了！[呼吸]座位嘛……是在山顶，哈哈，不过能去现场，我已经特别知足了。")

_sysprompt_cache: dict = {}    # role -> (prints_mtime, text)


def _rewriter_sysprompt(role: str) -> str:
    mt = _prints_cache["mtime"]
    c = _sysprompt_cache.get(role)
    if c and c[0] == mt:
        return c[1]
    p = _prints().get(role) or {}
    catch = "、".join((p.get("catch") or [])[:4]) or "（无）"
    # 手法段按角色分流（2026-07-31 主人定音「还可以更口语」后加强）：
    # - 普通聊天角色：上限抬高（想词 2~3、程度词叠 1~2、语气词隔一两句一个）+
    #   明确「拆碎口气」——探针实证长回复口语度塌方（48 分）主因是整句不拆。
    # - 自带 guide 的角色（硬汉/船长/播报）：照 guide 来，不硬塞想词——放开全角色
    #   灰度后这类角色进改写层，通用手法对他们是反人设。
    guide = p.get("guide") or ""
    if guide:
        craft = f"口语手法照该角色自己的方式来（不适合的手法别硬塞）：{guide}\n"
    else:
        craft = (
            "口语手法（大胆用，但别堆在同一句里）：长句拆碎成短口气，一口气尽量"
            "不超过十来个字（用逗号/省略号断开）；想词「嗯…、诶…、就是、那个」"
            "每段 2~3 处；程度词说两遍（真的真的/特别特别）1~2 处；"
            "说错改口（「周三…呃不对，周四」）长段最多 1 处；"
            "句尾语气词（呀/呢/啦/嘛/诶）隔一两句就带一个；"
            f"口头禅每段最多 1 个，从这里选：{catch}。\n")
    txt = (
        "你是口语化改写器：把一段数字人的回复改写成同一个意思的「微信语音逐字稿」，"
        "像随口说出来的话。\n"
        "硬规则（违反即废）：\n"
        "1. 事实一个字不许变：数字、英文、名字、时间、地点、肯定/否定立场原样保留"
        "（数字可以说成中文数目字但数值绝不能变；英文词和单位如 mAh、W、iPhone "
        "保持英文原样写法，不翻译成中文）；\n"
        "2. 不加新信息、不加承诺、不加对方称呼；\n"
        "3. 方括号标记（如 [呼吸]、【高兴】）原样保留在原位置，不新增不翻译；\n"
        "4. 长度不超过原文 1.5 倍；结尾收成完整句（句号/问号/感叹号/省略号），"
        "绝不停在半句；\n"
        "5. 只输出改写结果本身，不要任何解释或引号包裹。\n"
        + craft +
        f"角色说话指纹：{p.get('print', '自然闲聊风')}\n"
        "示例：\n" + _FEWSHOT)
    _sysprompt_cache[role] = (mt, txt)
    return txt


# ── 统计（进程内计数，INFO 日志逐条可查）────────────────────────────
stats = {"ok": 0, "timeout": 0, "reject": 0, "error": 0, "skip": 0}

_client = None


def _get_client():
    global _client
    if _client is None:
        import httpx
        _client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
    return _client


async def _llm_rewrite(role: str, text: str, timeout: float,
                       client=None) -> Optional[str]:
    payload = {
        "model": _LLM_MODEL,
        "messages": [{"role": "system", "content": _rewriter_sysprompt(role)},
                     {"role": "user", "content": text}],
        "temperature": 0.6,
        "max_tokens": max(96, min(640, len(text) * 2)),
        "stream": False,
    }
    r = await asyncio.wait_for(
        (client or _get_client()).post(_LLM_URL, json=payload), timeout=timeout)
    r.raise_for_status()
    out = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
    out = (out or "").strip().strip('"“”')
    # 万一模型带了前缀（「口语：」）——剥掉
    out = re.sub(r"^(口语|改写|输出)[:：]\s*", "", out).strip()
    # 发明标记机械回收（2026-08-01 09:51 监控实锤：few-shot 教「保留标记」的
    # 副作用=模型主动加 [呼吸]/【高兴】）。原文没有的标记直接剥掉——标记不念
    # 出来、剥除无损，比整段拒绝省一次口语化机会。「发明标记」锁保留作兜底。
    if out:
        _src_tags = set(_TAG_RE.findall(text))
        for _t in set(_TAG_RE.findall(out)) - _src_tags:
            out = out.replace(_t, "")
        out = re.sub(r"  +", " ", out).strip()
    # 情绪标记卫生（2026-08-01）：首尾标签在调用侧已摘走，产物里再冒出的任何
    # 情绪标记（含 [${情绪:…}] 变体）都是改写器自己发明的——直接剥掉。
    if out and "情绪" in out:
        out = _EMO_JUNK_RE.sub("", out).strip()
    # 吊半句收尾（2026-07-31 生产实录：「…对吧？您猜怎么着，」）——机械回剪到
    # 最后一个完整句终点；剪无可剪 → 判废走直通。
    if out and out[-1] in "，,、；;：:—":
        _cut = max(out.rfind(c) for c in "。！？!?…~")
        out = out[:_cut + 1] if _cut > 0 else ""
    return out or None


def build_rewrite_fn(role: str, flavor: float = 1.0):
    """Hub 侧构建：返回 async fn(text, first) -> Optional[str]；不适用返回 None。
    flavor<0.5（播报/清淡下限）不开——与出口注入同一档位语义，UI 零新概念。"""
    try:
        fl = 1.0 if flavor is None else float(flavor)
    except Exception:
        fl = 1.0
    if fl < 0.5 or not role_enabled(role):
        return None

    async def _fn(text: str, first: bool = False) -> Optional[str]:
        t = (text or "").strip()
        lead = tail = ""
        m = _LEAD_TAG_RE.match(t)
        if m:                               # 段首情绪标签：摘下→改写→接回
            lead, t = m.group(1), t[m.end():].strip()
        mt_ = _TAIL_TAG_RE.search(t)
        if mt_:                             # 段尾标签同族（分段挂尾）：摘尾→接回
            tail, t = mt_.group(1), t[:mt_.start()].rstrip()
        han = len(_HAN_RE.findall(t))
        if han < _MIN_HAN or _SKIP_RE.search(t) or han < len(t) * 0.4:
            stats["skip"] += 1
            return None                     # 短句/链接/非中文段直通
        t1, t2 = _timeouts()
        # 整轮/长段即使标 first 也用宽超时：CONV_TTS_UNIT=turn 时首段=全文，
        # 1.5~2.8s 铡刀会系统性误杀（冷启动尤甚）→ 听感回作文腔。
        budget = t2 if (not first or han >= 40) else t1
        t0 = time.time()
        try:
            out = await _llm_rewrite(role, t, budget)
        except asyncio.TimeoutError:
            stats["timeout"] += 1
            logger.info(f"[口语改写] 超时直通 role={role} first={first} "
                        f"budget={budget:.1f}s {len(t)}字 "
                        f"{(time.time() - t0) * 1000:.0f}ms")
            return None
        except Exception as e:
            stats["error"] += 1
            logger.info(f"[口语改写] 失败直通 role={role} {type(e).__name__}")
            return None
        if not out or not _fact_lock_ok(t, out):
            stats["reject"] += 1
            logger.info(f"[口语改写] 事实锁拒绝({_fact_lock_why(t, out or '')}) "
                        f"role={role} {len(t)}字→{len(out or '')}字 "
                        f"{(time.time() - t0) * 1000:.0f}ms src={t[:40]!r} "
                        f"dst={(out or '')[:40]!r}")
            return None
        stats["ok"] += 1
        logger.info(f"[口语改写] OK role={role} first={first} {len(t)}→{len(out)}字 "
                    f"{(time.time() - t0) * 1000:.0f}ms")
        return lead + out + tail if (lead or tail) else out

    return _fn


def rewrite_sync(role: str, text: str, timeout: float = 8.0,
                 debug: bool = False):
    """工具/巡检用同步入口（每次独立事件循环+独立客户端——AsyncClient 不可跨
    asyncio.run 复用；不给 Hub 用）。忽略旗标与档位，直接改写。
    debug=True 返回 {out, raw, why}（why=直通原因，事实锁逐项判点）。"""
    async def _run():
        import httpx
        t = (text or "").strip()
        # 首/尾标签摘接与生产 _fn 同款（离线测试必须镜像生产行为）
        lead = tail = ""
        m = _LEAD_TAG_RE.match(t)
        if m:
            lead, t = m.group(1), t[m.end():].strip()
        mt_ = _TAIL_TAG_RE.search(t)
        if mt_:
            tail, t = mt_.group(1), t[:mt_.start()].rstrip()
        info = {"out": None, "raw": None, "why": ""}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3.0)) as c:
            try:
                raw = await _llm_rewrite(role, t, timeout, client=c)
            except Exception as e:
                info["why"] = f"llm_error:{type(e).__name__}:{str(e)[:120]}"
                return info
        info["raw"] = raw
        if not raw:
            info["why"] = "empty"
            return info
        if _fact_lock_ok(t, raw):
            info["out"] = lead + raw + tail if (lead or tail) else raw
        else:
            info["why"] = "fact_lock:" + _fact_lock_why(t, raw)
        return info
    info = asyncio.run(_run())
    return info if debug else info["out"]


def _fact_lock_why(src: str, dst: str) -> str:
    """事实锁逐项判点（调参/巡检用；与 _fact_lock_ok 同一套规则）。"""
    if not dst or not dst.strip():
        return "空输出"
    sl, dl = len(src), len(dst)
    if dl > sl * 1.8 + 12:
        return f"过长 {sl}→{dl}"
    if dl < max(4, int(sl * 0.45)):
        return f"过短 {sl}→{dl}"
    _nm = _num_missing(src, dst)
    if _nm is not None:
        return f"数字丢失:{_nm}"
    for m in set(_ASCII_RE.findall(src)):
        if m.lower() not in dst.lower():
            return f"英文丢失:{m}"
    _ng = _neg_dropped(src, dst)
    if _ng is not None:
        return f"否定词减少:{_ng}"
    if _question_lost(src, dst):
        return "问句变陈述"
    src_tags = _TAG_RE.findall(src)
    for t in set(src_tags):
        if dst.count(t) != src_tags.count(t):
            return f"标记数变:{t}"
    for t in set(_TAG_RE.findall(dst)):
        if t not in src_tags:
            return f"发明标记:{t}"
    if _LEAK_RE.search(dst):
        return "自我暴露"
    return "?"
