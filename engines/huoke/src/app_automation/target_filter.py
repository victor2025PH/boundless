"""
目标用户筛选引擎 — 语言/国家/性别/年龄 多维度检测

两层漏斗:
  Layer 1 (文字快筛, 免费):  名字/bio/emoji/用户名模式/粉丝比例
  Layer 2 (AI精筛, 花钱):    截图发给云端 VLM 判断性别+年龄

用法:
    target = TargetProfile(country="italy", gender="male", min_age=30)
    signals = UserSignals(display_name="Marco Rossi", username="marco.rossi85",
                          bio="Imprenditore | Milano 🇮🇹")

    result = evaluate_user(signals, target)
    # result.is_match=True, result.score=0.92

    # 对不确定的用户, 用 AI 精筛:
    if result.needs_ai:
        ai_result = analyze_profile_screenshot(screenshot_bytes, target)
"""

from __future__ import annotations

import base64
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TargetProfile:
    """目标用户画像。所有字段可选，不设置的不检测。"""
    country: str = ""           # "italy", "germany", "france", etc.
    language: str = ""          # "italian", "german", "french"
    gender: str = ""            # "male" or "female"
    min_age: int = 0            # 最小年龄 (0=不限)
    max_age: int = 0            # 最大年龄 (0=不限)
    min_followers: int = 0      # 最少粉丝数
    max_followers: int = 0      # 最多粉丝数 (0=不限)
    min_score: float = 0.5      # 最低匹配分数 (0-1)
    countries: List[str] = field(default_factory=list)  # ISO codes: ['PH','ID','MY']
    languages: List[str] = field(default_factory=list)  # lang codes: ['tl','id','ms']


@dataclass
class UserSignals:
    """从 TikTok 用户资料页能提取的所有信号。"""
    display_name: str = ""
    username: str = ""
    bio: str = ""
    followers_count: int = -1   # -1=未知
    following_count: int = -1
    likes_count: int = -1
    video_count: int = -1


@dataclass
class MatchResult:
    is_match: bool
    score: float              # 0.0 - 1.0
    reasons: List[str]        # 匹配的原因列表
    disqualify: List[str]     # 不匹配的原因
    needs_ai: bool = False    # 建议用 AI 做二次确认 (不确定的情况)


# ═══════════════════════════════════════════════════════════════════════════
# 意大利语检测
# ═══════════════════════════════════════════════════════════════════════════

# 常见意大利男性名 (前100)
_ITALIAN_MALE_NAMES: Set[str] = {
    "marco", "giuseppe", "giovanni", "francesco", "alessandro", "andrea",
    "antonio", "luca", "roberto", "davide", "matteo", "stefano", "simone",
    "federico", "fabio", "riccardo", "paolo", "massimo", "giacomo", "claudio",
    "lorenzo", "filippo", "vincenzo", "daniele", "nicola", "michele", "salvatore",
    "emanuele", "carlo", "alberto", "enrico", "giorgio", "pietro", "mario",
    "luigi", "gianluca", "raffaele", "tommaso", "gabriele", "sergio",
    "mauro", "diego", "cristiano", "edoardo", "gianluigi", "angelo", "franco",
    "luciano", "renato", "giancarlo", "domenico", "enzo", "silvio", "dario",
    "adriano", "bruno", "cesare", "corrado", "donato", "elio", "erminio",
    "eugenio", "fausto", "fernando", "flavio", "gennaro", "gerardo",
    "giampiero", "gino", "guido", "italo", "ivano", "leon", "leonardo",
    "livio", "marcello", "mariano", "massimiliano", "mattia", "maurizio",
    "mirko", "nino", "nunzio", "osvaldo", "ottavio", "pasquale", "pier",
    "piero", "remo", "renzo", "rocco", "romeo", "ruggero", "sandro",
    "samuele", "tiziano", "tullio", "ugo", "valerio", "vittorio",
}

# 常见意大利女性名
_ITALIAN_FEMALE_NAMES: Set[str] = {
    "maria", "giulia", "francesca", "alessia", "chiara", "valentina", "sara",
    "anna", "elena", "silvia", "paola", "laura", "elisa", "martina",
    "federica", "simona", "roberta", "daniela", "monica", "claudia",
    "cristina", "ilaria", "patrizia", "barbara", "luisa", "giovanna",
    "rosa", "carmela", "angela", "sofia", "beatrice", "aurora", "alice",
    "giada", "greta", "irene", "noemi", "serena", "veronica", "bianca",
    "caterina", "arianna", "carlotta", "camilla", "emma", "eva", "mia",
    "viola", "margherita", "teresa", "lucia", "antonella", "grazia",
    "nicoletta", "ornella", "raffaella", "rita", "rossella", "sabrina",
    "stefania", "tiziana",
}

# 常见意大利姓氏
_ITALIAN_SURNAMES: Set[str] = {
    "rossi", "russo", "ferrari", "esposito", "bianchi", "romano", "colombo",
    "ricci", "marino", "greco", "bruno", "gallo", "conti", "costa",
    "giordano", "mancini", "rizzo", "lombardi", "moretti", "barbieri",
    "fontana", "santoro", "mariani", "rinaldi", "caruso", "ferrara",
    "galli", "martini", "leone", "longo", "gentile", "martinelli",
    "vitale", "lombardo", "serra", "coppola", "deangelis", "damico",
    "farina", "rizzi", "monti", "cattaneo", "morandi", "villa", "conte",
    "ferraro", "orlando", "pellegrini", "sanna", "fabbri", "marchetti",
    "grassi", "valentini", "palumbo", "messina", "sala", "delucia",
    "silvestri", "bernardi", "donati", "neri", "ruggiero", "caputo",
    "amato", "benedetti", "bianco", "parisi", "pagano", "piras",
}

# 意大利语高频词 (用于 bio/视频描述检测)
_ITALIAN_WORDS: Set[str] = {
    "sono", "vita", "amore", "lavoro", "italia", "italiano", "italiana",
    "milano", "roma", "napoli", "torino", "firenze", "bologna", "palermo",
    "genova", "venezia", "verona", "padova", "trieste", "bari", "catania",
    "padre", "mamma", "papà", "famiglia", "figlio", "figlia", "amici",
    "imprenditore", "imprenditrice", "fotografo", "musicista", "artista",
    "calcio", "squadra", "serie", "forza", "sempre", "tutto", "ogni",
    "della", "delle", "degli", "nello", "nella", "questo", "questa",
    "perché", "quando", "anche", "molto", "bene", "grazie", "ciao",
    "bellezza", "moda", "cucina", "mangiare", "buono", "bello", "bella",
    "ragazzo", "ragazza", "uomo", "donna", "giorno", "notte", "mondo",
    "cuore", "passione", "sogno", "libertà", "felicità",
}

# 意大利语 stop-word 组合 (出现2+个就很可能是意大利语)
_ITALIAN_STOPWORDS: Set[str] = {
    "il", "lo", "la", "le", "gli", "un", "una", "dei", "del",
    "di", "da", "in", "su", "per", "con", "tra", "fra",
    "che", "chi", "non", "più", "già", "mai", "ora",
    "sono", "sei", "siamo", "hanno", "mio", "tuo", "suo",
}


# ═══════════════════════════════════════════════════════════════════════════
# 全球多国检测：国旗+语言关键词
# ═══════════════════════════════════════════════════════════════════════════

# ISO code → flag emoji
_COUNTRY_FLAGS: Dict[str, str] = {
    'PH':'🇵🇭','ID':'🇮🇩','MY':'🇲🇾','TH':'🇹🇭','VN':'🇻🇳','SG':'🇸🇬',
    'MM':'🇲🇲','KH':'🇰🇭','LA':'🇱🇦','BN':'🇧🇳',
    'AE':'🇦🇪','SA':'🇸🇦','QA':'🇶🇦','KW':'🇰🇼','BH':'🇧🇭','OM':'🇴🇲',
    'IL':'🇮🇱','JO':'🇯🇴','EG':'🇪🇬','LB':'🇱🇧','IQ':'🇮🇶',
    'BR':'🇧🇷','MX':'🇲🇽','CO':'🇨🇴','AR':'🇦🇷','CL':'🇨🇱',
    'PE':'🇵🇪','VE':'🇻🇪','EC':'🇪🇨','BO':'🇧🇴',
    'US':'🇺🇸','GB':'🇬🇧','CA':'🇨🇦','AU':'🇦🇺','NZ':'🇳🇿','IE':'🇮🇪',
    'NG':'🇳🇬','GH':'🇬🇭','KE':'🇰🇪','ZA':'🇿🇦','TZ':'🇹🇿',
    'ET':'🇪🇹','UG':'🇺🇬','CM':'🇨🇲','SN':'🇸🇳',
    'DE':'🇩🇪','FR':'🇫🇷','IT':'🇮🇹','ES':'🇪🇸','PT':'🇵🇹',
    'NL':'🇳🇱','PL':'🇵🇱','RO':'🇷🇴','UA':'🇺🇦','RU':'🇷🇺','TR':'🇹🇷',
    'IN':'🇮🇳','PK':'🇵🇰','BD':'🇧🇩','LK':'🇱🇰',
    'JP':'🇯🇵','KR':'🇰🇷','TW':'🇹🇼',
}

# Language keyword sets for bio/name detection
_LANG_KEYWORDS: Dict[str, List[str]] = {
    'tl': ['pinoy','pilipinas','pilipino','tagalog','salamat','po','kuya','ate','mahal','mabuhay'],
    'id': ['indonesia','jakarta','surabaya','bandung','bismillah','alhamdulillah','mantap','wib','keren','aku'],
    'ms': ['malaysia','melayu','kuala lumpur','sabah','sarawak','kami','saya','awak','terima kasih'],
    'ar': ['عربي','مرحبا','الله','محمد','السلام','شكرا','إنشاء الله','حلال','يلا','اهلا'],
    'th': ['ไทย','กรุงเทพ','สวัสดี','ขอบคุณ','ครับ','ค่ะ','ไม่','ใจ'],
    'vi': ['việt','hà nội','hcm','xin chào','cảm ơn','bạn','mình','tôi'],
    'hi': ['india','hindi','namaste','delhi','mumbai','भारत','नमस्ते','हिंदी','yaar','bhai'],
    'ur': ['pakistan','urdu','karachi','lahore','اردو','پاکستان','آپ','ہے'],
    'bn': ['bangladesh','bangla','dhaka','বাংলা','ধন্যবাদ'],
    'ja': ['日本','東京','大阪','よろしく','ありがとう','おはよう','です','ます'],
    'ko': ['한국','서울','안녕','감사','대한민국','네','ㅋㅋ','진짜'],
    'zh': ['中国','台湾','香港','你好','谢谢','加油','哈哈','大家好'],
    'pt': ['brasil','são paulo','rio','obrigado','boa tarde','oi','tudo bem','tchau'],
    'es': ['mexico','colombia','argentina','hola','gracias','buenos','amor','hermano'],
    'fr': ['france','paris','bonjour','merci','salut','bonne','journée'],
    'de': ['deutschland','berlin','münchen','guten','danke','hallo','schön'],
    'it': ['italia','roma','milano','ciao','grazie','buongiorno','bello'],
    'sw': ['kenya','tanzania','habari','asante','karibu','rafiki','sawa'],
    'tr': ['türkiye','istanbul','ankara','merhaba','teşekkür','güzel','nasılsın'],
    'en': ['hello','thank you','follow','love','life','dream','hustle','grind','blessed'],
}

# ISO code → primary languages
_COUNTRY_PRIMARY_LANGS: Dict[str, List[str]] = {
    'PH':['tl','en'],'ID':['id'],'MY':['ms','en'],'TH':['th'],
    'VN':['vi'],'SG':['en','zh'],'MM':['my'],'KH':['km'],
    'AE':['ar'],'SA':['ar'],'QA':['ar'],'KW':['ar'],'BH':['ar'],'OM':['ar'],
    'IL':['he','ar'],'JO':['ar'],'EG':['ar'],'LB':['ar'],
    'BR':['pt'],'MX':['es'],'CO':['es'],'AR':['es'],'CL':['es'],
    'US':['en'],'GB':['en'],'CA':['en','fr'],'AU':['en'],'NZ':['en'],
    'NG':['en'],'GH':['en'],'KE':['sw','en'],'ZA':['en'],'TZ':['sw'],
    'DE':['de'],'FR':['fr'],'IT':['it'],'ES':['es'],'PT':['pt'],
    'NL':['nl'],'PL':['pl'],'RU':['ru'],'UA':['uk'],'TR':['tr'],
    'IN':['hi','en'],'PK':['ur'],'BD':['bn'],'LK':['si','ta'],
    'JP':['ja'],'KR':['ko'],'TW':['zh'],
}

# Legacy country name → ISO code
_COUNTRY_NAME_TO_CODE: Dict[str, str] = {
    'italy':'IT','germany':'DE','france':'FR','spain':'ES','uk':'GB','usa':'US',
    'brazil':'BR','japan':'JP','korea':'KR','australia':'AU','canada':'CA',
    'philippines':'PH','indonesia':'ID','malaysia':'MY','thailand':'TH',
    'vietnam':'VN','singapore':'SG','uae':'AE','saudi_arabia':'SA',
    'nigeria':'NG','kenya':'KE','india':'IN','taiwan':'TW','mexico':'MX',
    'colombia':'CO','argentina':'AR',
}


def detect_italian_text(text: str) -> Tuple[bool, float, List[str]]:
    """
    检测文本是否为意大利语。

    Returns:
        (is_italian, confidence, clues)
    """
    if not text:
        return False, 0.0, []

    text_lower = text.lower()
    words = set(re.findall(r'\b[a-zà-ú]{2,}\b', text_lower))
    clues = []

    # 🇮🇹 国旗 emoji
    if "🇮🇹" in text:
        clues.append("flag:🇮🇹")

    # 意大利城市/地名
    italian_word_hits = words & _ITALIAN_WORDS
    if italian_word_hits:
        clues.append(f"words:{','.join(list(italian_word_hits)[:3])}")

    # 意大利语 stop-word 频率
    stopword_hits = words & _ITALIAN_STOPWORDS
    if len(stopword_hits) >= 2:
        clues.append(f"grammar:{len(stopword_hits)} stopwords")

    score = 0.0
    if "flag:🇮🇹" in str(clues):
        score += 0.5
    score += min(len(italian_word_hits) * 0.15, 0.5)
    score += min(len(stopword_hits) * 0.1, 0.3)
    score = min(score, 1.0)

    return score >= 0.3, score, clues


def detect_italian_name(display_name: str) -> Tuple[Optional[str], float]:
    """
    从显示名检测是否为意大利名字。

    Returns:
        (gender or None, confidence)
        gender: "male", "female", None
    """
    parts = re.findall(r'[a-zà-ú]+', display_name.lower())
    if not parts:
        return None, 0.0

    first = parts[0]
    has_surname = False
    for p in parts:
        if p in _ITALIAN_SURNAMES:
            has_surname = True
            break

    if first in _ITALIAN_MALE_NAMES:
        conf = 0.8 if has_surname else 0.6
        return "male", conf

    if first in _ITALIAN_FEMALE_NAMES:
        conf = 0.8 if has_surname else 0.6
        return "female", conf

    # 只有姓没有名
    if has_surname and not first in _ITALIAN_MALE_NAMES | _ITALIAN_FEMALE_NAMES:
        return None, 0.3

    return None, 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 年龄估算
# ═══════════════════════════════════════════════════════════════════════════

_BIRTH_YEAR_PATTERN = re.compile(r'(?:^|[^0-9])(19[5-9]\d|200\d)(?:$|[^0-9])')
_YOUNG_INDICATORS = re.compile(
    r'\b(\d{1,2}\s*y/?o|teen|student|school|15|16|17|18|19|classe)\b',
    re.IGNORECASE
)
_MATURE_INDICATORS = re.compile(
    r'\b(imprenditor|manager|founder|ceo|padre|papà|father|dad|'
    r'professionista|avvocato|medico|ingegnere|architetto|dottore|'
    r'coach|consulente|business|azienda|esperienza|'
    r'husband|moglie|marito|nonno|nonna)\b',
    re.IGNORECASE
)


def estimate_age(username: str, bio: str) -> Tuple[Optional[int], float, str]:
    """
    估算用户年龄。

    检测方法:
    1. 用户名中的出生年份数字 (e.g. marco1985 → 41岁)
    2. Bio中的成熟度指标 (职业、家庭角色)
    3. Bio中的年轻指标 (student、teen)

    Returns:
        (estimated_age or None, confidence, method)
    """
    now_year = datetime.now().year

    # Method 1: 用户名中的出生年份
    combined = f"{username} {bio}"
    m = _BIRTH_YEAR_PATTERN.search(combined)
    if m:
        birth_year = int(m.group(1))
        age = now_year - birth_year
        if 10 <= age <= 100:
            return age, 0.8, f"birth_year:{birth_year}"

    # Method 2: 成熟度指标
    if _MATURE_INDICATORS.search(bio):
        return 35, 0.4, "mature_bio"

    # Method 3: 年轻指标
    if _YOUNG_INDICATORS.search(bio):
        return 18, 0.4, "young_bio"

    # Method 4: 用户名尾部2位数字可能是年份
    m2 = re.search(r'(\d{2})$', username)
    if m2:
        num = int(m2.group(1))
        if 50 <= num <= 99:
            age = now_year - (1900 + num)
            if 20 <= age <= 80:
                return age, 0.5, f"username_suffix:19{num}"
        elif 0 <= num <= 10:
            age = now_year - (2000 + num)
            if 15 <= age <= 30:
                return age, 0.4, f"username_suffix:20{num:02d}"

    return None, 0.0, "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# 综合评估
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# Hashtag 策略: 用什么搜索词能高效找到目标群体
# ═══════════════════════════════════════════════════════════════════════════

# 国家 → 推荐搜索词 (hashtags + 关键词)
COUNTRY_SEARCH_TERMS = {
    "italy": [
        "#italia", "#italiano", "#italiani", "#madeinitaly",
        "#roma", "#milano", "#napoli", "#firenze",
        "italiano", "italia calcio", "cucina italiana",
    ],
    "germany": [
        "#deutschland", "#deutsch", "#german",
        "#berlin", "#münchen", "#hamburg",
        "deutsch", "deutschland",
    ],
    "france": [
        "#france", "#français", "#paris",
        "#lyon", "#marseille",
        "français", "france",
    ],
    "spain": [
        "#españa", "#español", "#madrid",
        "#barcelona", "#sevilla",
        "español", "españa",
    ],
    "brazil": [
        "#brasil", "#brasileiro", "#saopaulo",
        "#riodejaneiro",
        "brasileiro", "brasil",
    ],
    "india": [
        "#india", "#hindi", "#mumbai",
        "#delhi", "#bangalore",
        "india", "hindi",
    ],
    "middle_east": [
        "#arab", "#arabic", "#dubai",
        "#riyadh", "#egypt",
        "arabic", "عربي",
    ],
}

def get_search_terms(country: str) -> List[str]:
    return COUNTRY_SEARCH_TERMS.get(country, [country])


# ═══════════════════════════════════════════════════════════════════════════
# 增强信号: Emoji / 用户名模式 / 粉丝比例
# ═══════════════════════════════════════════════════════════════════════════

# 偏男性的 emoji
_MALE_EMOJIS = set("💪🏋️⚽🏀🎮🕹️🏆🥊🏈⚾🏒🔧🔨🛠️🔥👊🤙🏎️🚗💰📈🎯🍺🥃🎸🤘")
# 偏女性的 emoji
_FEMALE_EMOJIS = set("💅💄👗👠💋🌸🌺🌷💐🦋🧚✨💖💝🩷🎀👩🧘‍♀️🧖‍♀️💃🌈🦄🪷🫧🌙")

# 用户名中的性别暗示
_USERNAME_MALE_PAT = re.compile(
    r'(boy|man|king|boss|dude|bro|sir|mr|padre|papa|dad|father|guy|ragazzo|uomo|signor)',
    re.IGNORECASE)
_USERNAME_FEMALE_PAT = re.compile(
    r'(girl|woman|queen|princess|lady|miss|mrs|madre|mamma|mom|mother|gal|ragazza|donna|bella)',
    re.IGNORECASE)


def detect_gender_from_emoji(text: str) -> Tuple[Optional[str], float]:
    """从 emoji 使用倾向推断性别。"""
    male_count = sum(1 for ch in text if ch in _MALE_EMOJIS)
    female_count = sum(1 for ch in text if ch in _FEMALE_EMOJIS)
    total = male_count + female_count
    if total == 0:
        return None, 0.0
    if male_count > female_count:
        return "male", min(0.3 + (male_count - female_count) * 0.1, 0.6)
    elif female_count > male_count:
        return "female", min(0.3 + (female_count - male_count) * 0.1, 0.6)
    return None, 0.0


def detect_gender_from_username(username: str) -> Tuple[Optional[str], float]:
    """从用户名模式推断性别。"""
    if _USERNAME_MALE_PAT.search(username):
        return "male", 0.5
    if _USERNAME_FEMALE_PAT.search(username):
        return "female", 0.5
    return None, 0.0


def estimate_age_from_activity(followers: int, following: int,
                               likes: int, video_count: int) -> Tuple[Optional[str], float]:
    """
    从账号活跃度指标推断年龄段。

    经验规律:
    - 年轻人 (<25): 关注多, 粉丝少, 点赞多, 发布频繁
    - 成熟用户 (30+): 关注少, 粉丝/关注比高, 视频少但质量高
    - 商务用户 (35+): 粉丝数适中, 关注极少, 内容专业
    """
    if followers < 0 or following < 0:
        return None, 0.0

    ratio = followers / max(following, 1)

    # 关注了非常多人但粉丝很少 → 年轻/新号
    if following > 500 and followers < 100:
        return "young", 0.3

    # 几乎不关注别人但有粉丝 → 成熟/商务
    if following < 50 and followers > 200:
        return "mature", 0.3

    # 关注数适中, 粉丝比例健康 → 无法判断
    return None, 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 增强版综合评估 (替换原版)
# ═══════════════════════════════════════════════════════════════════════════

def _detect_country_generic(country_code: str, signals) -> float:
    """Generic country detection using flag emoji + language keyword heuristics.
    Returns confidence score 0.0–1.0."""
    code = country_code.upper()
    score = 0.0
    bio_name = (getattr(signals, 'bio', '') + ' ' + getattr(signals, 'display_name', '') +
                ' ' + getattr(signals, 'username', '')).lower()

    # Signal 1: Flag emoji in bio/display_name (strong signal, 0.75)
    flag = _COUNTRY_FLAGS.get(code, '')
    if flag and flag in (getattr(signals, 'bio', '') + getattr(signals, 'display_name', '')):
        score = max(score, 0.78)

    # Signal 2: Language keywords matching country's primary languages (medium signal)
    primary_langs = _COUNTRY_PRIMARY_LANGS.get(code, [])
    for lang in primary_langs:
        keywords = _LANG_KEYWORDS.get(lang, [])
        matches = sum(1 for kw in keywords if kw in bio_name)
        if matches >= 3:
            score = max(score, 0.65)
        elif matches >= 2:
            score = max(score, 0.5)
        elif matches == 1:
            score = max(score, 0.3)

    return score


def evaluate_user(signals: UserSignals, target: TargetProfile) -> MatchResult:
    """
    综合评估用户是否符合目标画像。

    使用所有可用信号:
    - 名字数据库 (最可靠)
    - Bio 文本语言检测
    - Emoji 使用倾向
    - 用户名关键词模式
    - 出生年份 (用户名中的数字)
    - Bio 成熟度关键词
    - 粉丝/关注比例
    """
    reasons = []
    disqualify = []
    scores = {}
    gender_votes: List[Tuple[str, float]] = []  # (gender, confidence)
    age_votes: List[Tuple[int, float]] = []     # (age, confidence)

    # ── 1. 国家/语言检测 ──
    if target.country == "italy" or target.language == "italian":
        name_gender, name_conf = detect_italian_name(signals.display_name)
        bio_italian, bio_conf, bio_clues = detect_italian_text(signals.bio)
        _, _, username_clues = detect_italian_text(
            signals.username.replace(".", " ").replace("_", " "))

        country_score = 0.0
        if name_conf > 0:
            country_score = max(country_score, name_conf)
            first = signals.display_name.split()[0] if signals.display_name else "?"
            reasons.append(f"Italian name ({first})")
            if name_gender:
                gender_votes.append((name_gender, name_conf))
        if bio_italian:
            country_score = max(country_score, bio_conf)
            reasons.append(f"bio:Italian ({','.join(bio_clues[:2])})")
        if "🇮🇹" in (signals.bio + signals.display_name):
            country_score = max(country_score, 0.7)
            reasons.append("flag:🇮🇹")
        if username_clues:
            country_score = max(country_score, 0.3)

        scores["country"] = country_score
        if country_score < 0.2:
            disqualify.append("not Italian")

    # ── 1b. 多国通用检测 (新: countries 字段 + 非Italy country字段) ──
    # Multi-country: check if user matches ANY target country
    _multi_codes = list(getattr(target, 'countries', []) or [])
    # Also handle legacy country field for non-Italy
    if target.country and target.country.lower() not in ('italy',) and 'country' not in scores:
        _legacy_code = _COUNTRY_NAME_TO_CODE.get(target.country.lower(), target.country.upper()[:2])
        if _legacy_code and _legacy_code not in _multi_codes:
            _multi_codes.append(_legacy_code)

    if _multi_codes and 'country' not in scores:
        # Score = max match across all target countries (OR logic)
        _max_geo_score = 0.0
        for _code in _multi_codes:
            # Special case: Italy already handled above
            if _code.upper() == 'IT':
                continue
            _s = _detect_country_generic(_code, signals)
            _max_geo_score = max(_max_geo_score, _s)

        # If Italy is also in the list, use max of Italy score and generic score
        if 'IT' in [c.upper() for c in _multi_codes] and 'country' in scores:
            _max_geo_score = max(_max_geo_score, scores.get('country', 0))

        scores['country'] = _max_geo_score
        if _max_geo_score < 0.15:
            disqualify.append('not target country')

    # ── 2. 性别检测 (多信号投票) ──
    if target.gender:
        # 信号源 1: 名字数据库 (已在上面收集)
        # 信号源 2: Bio 关键词
        bio_lower = (signals.bio + " " + signals.display_name).lower()
        male_kw = ["uomo", "ragazzo", "padre", "papà", "father", "dad",
                    "husband", "marito", "him", "his", "man", "mr",
                    "businessman", "imprenditore", "fotografo"]
        female_kw = ["donna", "ragazza", "madre", "mamma", "mother",
                     "mom", "wife", "moglie", "her", "she", "woman", "mrs",
                     "imprenditrice", "fotografa"]
        male_bio = sum(1 for w in male_kw if w in bio_lower)
        female_bio = sum(1 for w in female_kw if w in bio_lower)
        if male_bio > female_bio:
            gender_votes.append(("male", 0.4 + male_bio * 0.1))
            reasons.append("bio:male_keywords")
        elif female_bio > male_bio:
            gender_votes.append(("female", 0.4 + female_bio * 0.1))

        # 信号源 3: Emoji 倾向
        emoji_gender, emoji_conf = detect_gender_from_emoji(
            signals.bio + signals.display_name)
        if emoji_gender:
            gender_votes.append((emoji_gender, emoji_conf))
            if emoji_gender == target.gender:
                reasons.append(f"emoji:{emoji_gender}")

        # 信号源 4: 用户名模式
        un_gender, un_conf = detect_gender_from_username(signals.username)
        if un_gender:
            gender_votes.append((un_gender, un_conf))
            if un_gender == target.gender:
                reasons.append(f"username:{un_gender}")

        # 投票汇总
        if gender_votes:
            male_score = sum(c for g, c in gender_votes if g == "male")
            female_score = sum(c for g, c in gender_votes if g == "female")
            total_conf = male_score + female_score

            if total_conf > 0:
                if target.gender == "male":
                    scores["gender"] = male_score / total_conf
                else:
                    scores["gender"] = female_score / total_conf

                # 如果有强烈反向信号 → 硬性否决
                winning = "male" if male_score > female_score else "female"
                if winning != target.gender and max(male_score, female_score) > 0.5:
                    disqualify.append(f"gender:{winning} (wanted {target.gender})")
                elif winning == target.gender:
                    reasons.append(target.gender)
            else:
                scores["gender"] = 0.3
        else:
            scores["gender"] = 0.3  # 无任何信号

    # ── 3. 年龄检测 (多信号) ──
    if target.min_age > 0 or target.max_age > 0:
        # 信号源 1: 用户名/bio 中的出生年份
        est_age, age_conf, age_method = estimate_age(
            signals.username, signals.bio)
        if est_age is not None:
            age_votes.append((est_age, age_conf))

        # 信号源 2: 账号活跃度
        activity_age, act_conf = estimate_age_from_activity(
            signals.followers_count, signals.following_count,
            signals.likes_count, signals.video_count)
        if activity_age == "mature":
            age_votes.append((35, act_conf))
        elif activity_age == "young":
            age_votes.append((20, act_conf))

        # 汇总
        if age_votes:
            weighted_age = sum(a * c for a, c in age_votes) / sum(c for _, c in age_votes)
            best_conf = max(c for _, c in age_votes)
            est = int(weighted_age)

            age_ok = True
            if target.min_age > 0 and est < target.min_age:
                if best_conf > 0.5:
                    age_ok = False
                    disqualify.append(f"age~{est} (min:{target.min_age})")
            if target.max_age > 0 and est > target.max_age:
                if best_conf > 0.5:
                    age_ok = False
                    disqualify.append(f"age~{est} (max:{target.max_age})")

            if age_ok:
                scores["age"] = best_conf
                for a, c in age_votes:
                    if c >= 0.4:
                        reasons.append(f"age~{a}")
                        break
            else:
                scores["age"] = 0.0
        else:
            scores["age"] = 0.3  # 无法判断

    # ── 4. 粉丝数检测 ──
    if target.min_followers > 0 or target.max_followers > 0:
        if signals.followers_count >= 0:
            fol_ok = True
            if target.min_followers > 0 and signals.followers_count < target.min_followers:
                fol_ok = False
            if target.max_followers > 0 and signals.followers_count > target.max_followers:
                fol_ok = False
            scores["followers"] = 1.0 if fol_ok else 0.0
        else:
            scores["followers"] = 0.5

    # ── 计算总分 ──
    if not scores:
        return MatchResult(True, 1.0, ["no_filter"], [], False)

    weights = {"country": 0.35, "gender": 0.25, "age": 0.25, "followers": 0.15}
    total_weight = sum(weights.get(k, 0.1) for k in scores)
    final_score = sum(scores.get(k, 0) * weights.get(k, 0.1)
                      for k in scores) / max(total_weight, 0.01)

    hard_fail = any("gender:" in d and "wanted" in d for d in disqualify)
    hard_fail = hard_fail or any("age~" in d and "min:" in d for d in disqualify)
    hard_fail = hard_fail or any("not Italian" in d for d in disqualify) or any("not target country" in d for d in disqualify)

    is_match = final_score >= target.min_score and not hard_fail

    # 判断是否需要 AI 二次确认
    gender_uncertain = scores.get("gender", 1) <= 0.35
    age_uncertain = scores.get("age", 1) <= 0.35
    country_ok = scores.get("country", 0) >= 0.3
    needs_ai = country_ok and (gender_uncertain or age_uncertain) and not hard_fail

    return MatchResult(is_match, round(final_score, 2), reasons, disqualify, needs_ai)


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2: VLM 头像/资料页截图分析
# ═══════════════════════════════════════════════════════════════════════════

_VLM_PROMPT = """Analyze this TikTok user profile screenshot. Answer ONLY in this exact JSON format:
{"gender": "male" or "female" or "unknown", "age_range": "under25" or "25-35" or "35-50" or "over50" or "unknown", "confidence": 0.0-1.0}

Clues to look for:
- Profile photo: face shape, hair, beard/mustache, makeup
- Display name style
- Bio text content and language
- Overall aesthetic of the profile

Be concise. Output ONLY the JSON, nothing else."""


def analyze_profile_screenshot(screenshot_bytes: bytes,
                               target: TargetProfile,
                               llm_client=None) -> MatchResult:
    """
    用 VLM 分析用户资料页截图, 判断性别和年龄。

    自动选择免费 provider:
      1. Google Gemini (免费 1500次/天)
      2. Ollama 本地 (免费无限次)
      3. 回退到默认 LLM

    Args:
        screenshot_bytes: PNG 格式的资料页截图
        target: 目标画像
        llm_client: LLMClient 实例 (None=自动选择免费)

    Returns:
        MatchResult with AI-based determination
    """
    if llm_client is None:
        try:
            from ..ai.llm_client import get_free_vision_client, get_llm_client
            llm_client = get_free_vision_client() or get_llm_client()
        except Exception as e:
            log.warning("LLM client unavailable: %s", e)
            return MatchResult(False, 0.0, [], ["ai_unavailable"], False)

    try:
        img_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        response = llm_client.chat_vision(_VLM_PROMPT, img_b64, max_tokens=1024)

        import json as _json
        # 提取 JSON — 兼容 Gemini 的 ```json ``` 包裹格式
        cleaned = re.sub(r'```(?:json)?\s*', '', response).strip()
        json_match = re.search(r'\{[^}]+\}', cleaned)
        if not json_match:
            log.debug("AI response no JSON: %s", response[:200])
            return MatchResult(False, 0.0, [], ["ai_parse_error"], False)

        data = _json.loads(json_match.group())
        ai_gender = data.get("gender", "unknown")
        ai_age_range = data.get("age_range", "unknown")
        ai_conf = float(data.get("confidence", 0.5))

        reasons = []
        disqualify = []

        # 性别判定
        gender_ok = True
        if target.gender and ai_gender != "unknown":
            if ai_gender == target.gender:
                reasons.append(f"ai_gender:{ai_gender}")
            else:
                gender_ok = False
                disqualify.append(f"ai_gender:{ai_gender} (wanted {target.gender})")

        # 年龄判定
        age_ok = True
        age_map = {"under25": 22, "25-35": 30, "35-50": 42, "over50": 55}
        est_age = age_map.get(ai_age_range)
        if est_age and target.min_age > 0:
            if est_age < target.min_age and ai_age_range != "unknown":
                age_ok = False
                disqualify.append(f"ai_age:{ai_age_range} (min:{target.min_age})")
            else:
                reasons.append(f"ai_age:{ai_age_range}")

        is_match = gender_ok and age_ok and ai_conf >= 0.3
        score = ai_conf if is_match else ai_conf * 0.3

        return MatchResult(is_match, round(score, 2), reasons, disqualify, False)

    except Exception as e:
        log.warning("VLM analysis failed: %s", e)
        return MatchResult(False, 0.0, [], [f"ai_error:{e}"], False)
