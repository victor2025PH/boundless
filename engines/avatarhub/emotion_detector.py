"""
emotion_detector.py — Phase 4: 智能情感匹配
根据文本内容自动选择最合适的情感标签，供 avatar_hub /avatar/speak emotion='auto' 使用。

支持的情感（与 CosyVoice3 / emotion_tts_server 一致）:
  neutral / happy / sad / angry / fearful / surprised /
  disgusted / gentle / excited / calm / serious
"""
import re

# ── 关键词规则库 ─────────────────────────────────────────────────────
_RULES: dict[str, dict] = {
    "excited": {
        "zh": ["太棒了", "哇塞", "好激动", "爱死了", "超厉害", "绝了", "牛啊", "耶", "哇哦", "帅炸",
               "太好了", "真的吗", "天才", "无敌", "赢了", "成功了",
               # P6 扩充：带货/直播营销语境（P4-4 实测「注意查收优惠券」被 serious 的「注意」
               # 抢走——营销词按语境归热情，权重 3 盖过 serious 的 2）
               "家人们", "优惠券", "下单", "秒杀", "上链接", "抢购", "闭眼入", "福利",
               "划算", "性价比", "手慢无", "冲鸭"],
        "en": ["awesome", "amazing", "fantastic", "so excited", "incredible", "yay", "oh my god",
               "cant believe", "unreal", "genius", "won", "victory"],
        "punct_re": r"[!！]{2,}",
        "weight": 3,
    },
    "happy": {
        "zh": ["开心", "快乐", "高兴", "喜欢", "爱你", "好玩", "有趣", "嘿嘿", "嘻嘻", "哈哈",
               "笑", "乐", "棒", "好呀", "好的好的", "太好了", "真好", "喜悦", "幸福",
               "不错", "挺好", "心情好", "很好", "蛮好", "还行", "心情不错"],
        "en": ["happy", "glad", "delighted", "pleased", "love", "enjoy", "fun", "wonderful",
               "joyful", "cheerful", "smile", "laugh", "great", "nice", "good"],
        "weight": 2,
    },
    "sad": {
        "zh": ["伤心", "难过", "哭了", "泪", "痛苦", "悲伤", "遗憾", "失望", "可怜", "孤独",
               "想念", "思念", "舍不得", "心碎", "不舍", "哭泣", "流泪", "心疼",
               # 9-11 扩充：低落/疲惫/抑郁类（用多字短语避免「累」「丧」等单字误命中）
               "郁闷", "压抑", "低落", "好丧", "不开心", "难受", "委屈", "心累", "好累",
               "疲惫", "沮丧", "失落", "提不起劲", "没动力", "心情不好", "心情低落",
               "不想说话", "什么都不想做", "撑不下去", "好难"],
        "en": ["sad", "cry", "tears", "lonely", "miss", "heartbroken", "disappointed", "sorrow",
               "grief", "mourn", "depressed", "unhappy", "regret",
               "exhausted", "burned out", "burnt out", "worn out", "miserable",
               "hopeless", "feeling down"],
        "weight": 2,
    },
    "angry": {
        "zh": ["气死", "愤怒", "生气", "烦透了", "讨厌", "混蛋", "真烦", "气人", "烦死了", "无语",
               "不可理喻", "岂有此理", "凭什么", "滚", "闭嘴", "太过分了", "不像话",
               # 9-11 扩充：烦躁/受够类（实测「烦躁/有点烦」此前漏判为 neutral）
               "烦躁", "心烦", "有点烦", "好烦", "烦人", "闹心", "抓狂", "受够了", "受不了"],
        "en": ["angry", "furious", "irritated", "annoyed", "hate", "rage", "outrageous",
               "ridiculous", "shut up", "damn", "unacceptable",
               "fed up", "frustrated", "annoying"],
        "weight": 4,   # 愤怒词命中时权重更高
    },
    "fearful": {
        "zh": ["害怕", "恐惧", "吓到", "担心", "紧张", "不安", "颤抖", "战战兢兢", "不敢", "心跳",
               "慌了", "怎么办", "完了", "糟了", "怕怕",
               # 9-11 扩充：焦虑/压力类
               "焦虑", "扛不住", "撑不住", "压力大", "忐忑", "好慌", "睡不着"],
        "en": ["scared", "afraid", "fear", "terrified", "nervous", "anxious", "worried",
               "panic", "dread", "frighten", "trembling", "stressed", "overwhelmed"],
        "weight": 2,
    },
    "surprised": {
        "zh": ["没想到", "竟然", "天啊", "居然", "不可思议", "震惊", "意外", "不敢相信", "啊啊",
               "什么", "这也行", "真的假的",
               # P6 扩充：吐槽式惊讶（P4-4 实测「这价格也太离谱了吧」漏判 neutral）
               "离谱", "太夸张"],
        "en": ["surprised", "shocked", "unbelievable", "unexpected", "no way", "really",
               "seriously", "what the", "cant believe", "omg"],
        "weight": 2,
    },
    "disgusted": {
        "zh": ["恶心", "恶臭", "呕吐", "恶", "脏", "臭", "丑陋", "令人作呕", "太难看了", "丑死了"],
        "en": ["disgusting", "gross", "filthy", "nasty", "repulsive", "revolting", "yuck",
               "eww", "awful", "horrible looking"],
        "weight": 2,
    },
    "gentle": {
        "zh": ["好的", "请", "谢谢", "麻烦", "辛苦了", "温柔", "轻声", "感谢", "劳烦", "拜托",
               "不好意思", "能否", "可以吗", "帮我", "请问", "您好",
               # P6 扩充：安抚/陪伴语境（P4-4 实测「别怕，有我在」漏判 neutral）+ 晚安电台
               "别怕", "有我在", "别担心", "我陪你", "没事的", "别哭", "安心", "放宽心",
               "晚安", "好梦"],
        "en": ["please", "thank", "gently", "softly", "kindly", "grateful", "excuse me",
               "could you", "would you mind", "appreciate"],
        "weight": 2,
    },
    "serious": {
        "zh": ["注意", "警告", "严肃", "重要", "必须", "禁止", "规定", "严格", "正式", "通知",
               "特此", "特别提醒", "务必", "严禁", "依法"],
        "en": ["warning", "important", "critical", "serious", "must", "forbidden", "official",
               "notify", "announcement", "mandatory", "strictly"],
        "weight": 2,
    },
    "calm": {
        "zh": ["平静", "安静", "冷静", "淡定", "放松", "休息", "深呼吸", "慢慢来", "不急",
               "好好的", "一切都好", "没关系"],
        "en": ["calm", "relax", "peaceful", "quiet", "serene", "tranquil", "take it easy",
               "no worries", "it is fine", "settle down"],
        "weight": 2,
    },
}

# 候选情感（neutral 不在规则表里，作为 fallback）
_ALL_EMOTIONS = {"neutral", "happy", "sad", "angry", "fearful",
                 "surprised", "disgusted", "gentle", "excited", "calm", "serious"}


def _compute_scores(text: str) -> dict[str, float]:
    """关键词 + 标点打分（detect_emotion 与 detail 共用，避免两处规则漂移）。"""
    text_lower = text.lower()
    scores: dict[str, float] = {e: 0.0 for e in _RULES}
    for emotion, rules in _RULES.items():
        w = rules.get("weight", 2)
        for kw in rules.get("zh", []):
            if kw in text:
                scores[emotion] += w
        for kw in rules.get("en", []):
            if kw in text_lower:
                scores[emotion] += w
        pat = rules.get("punct_re")
        if pat and re.search(pat, text):
            scores[emotion] += w + 1   # 标点强化
    # 连续感叹号 → excited 加权
    excl_count = len(re.findall(r"[!！]", text))
    if excl_count >= 3:
        scores["excited"] += 3
    elif excl_count == 2:
        scores["excited"] += 1
    # 问号连串 → surprised
    ques_count = len(re.findall(r"[?？]", text))
    if ques_count >= 2:
        scores["surprised"] += 1
    return scores


def detect_emotion(text: str) -> str:
    """
    分析文本，返回最匹配的情感标签。
    无明显情感特征时返回 "neutral"。

    Args:
        text: 待分析的文字（中文/英文均可）

    Returns:
        情感字符串，取自 _ALL_EMOTIONS
    """
    if not text or len(text.strip()) < 2:
        return "neutral"

    scores = _compute_scores(text)
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return "neutral"

    # 低置信度保护：多情感竞争时不强行输出
    total = sum(scores.values())
    confidence = scores[best] / total if total > 0 else 0.0
    if confidence < 0.25:
        return "neutral"

    return best


def detect_emotion_detail(text: str) -> dict:
    """
    返回详细得分（用于 /api/emotion_detect 端点调试）。
    """
    if not text or len(text.strip()) < 2:
        return {"emotion": "neutral", "scores": {}, "confidence": 0.0}

    scores = _compute_scores(text)
    total = sum(scores.values())
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        best = "neutral"

    top_scores = dict(sorted(scores.items(), key=lambda x: -x[1])[:5])
    confidence = round(scores[best] / total, 3) if total > 0 else 0.0

    return {
        "emotion": best,
        "scores": top_scores,
        "confidence": confidence,
        "total_signals": int(total),
    }


if __name__ == "__main__":
    tests = [
        ("今天太开心了！！！真的太棒了！！", "excited"),
        ("我好伤心，眼泪都流下来了", "sad"),
        ("气死我了！！你这混蛋！", "angry"),
        ("谢谢您的帮助，麻烦您了", "gentle"),
        ("注意！以下事项必须严格遵守", "serious"),
        ("没想到竟然是这样，真不可思议", "surprised"),
        ("今天天气很好，心情还不错", "happy"),
        ("好吧，那就这样吧", "neutral"),
        # 9-11 新增：此前漏判为 neutral 的负面口语
        ("我最近好烦躁，什么都不顺", "angry"),
        ("有点烦，不想说话", "angry"),
        ("好累啊，感觉撑不住了", "sad"),
        ("最近压力大，有点焦虑", "fearful"),
        ("心情低落，提不起劲", "sad"),
        # P6 新增：带货/安抚/吐槽惊讶（P4-4 实验实锤的漏判，规则层能修的部分）
        ("注意查收优惠券哦亲！", "excited"),
        ("家人们！今天这款宝贝真的可以闭眼入！", "excited"),
        ("别怕，有我在。", "gentle"),
        ("这价格也太离谱了吧！", "surprised"),
        ("愿你今晚有个好梦，晚安。", "gentle"),
        ("明天记得带伞。", "neutral"),
    ]
    print("emotion_detector 自测:")
    ok = 0
    for text, expected in tests:
        got = detect_emotion(text)
        mark = "OK" if got == expected else f"FAIL(expected:{expected})"
        print(f"  [{mark}] [{got}] {text[:20]}")
        if got == expected:
            ok += 1
    print(f"\n结果: {ok}/{len(tests)}")
