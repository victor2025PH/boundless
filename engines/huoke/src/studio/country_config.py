# -*- coding: utf-8 -*-
"""
国家内容策略配置 — 按国家/地区定义内容风格、语言、文化偏好。

使用方式:
    from src.studio.country_config import get_country_config
    cfg = get_country_config("italy")
    voice = get_tts_voice("brazil", gender="female")
    hours = get_peak_utc_hours("japan")
"""

from typing import Dict, Any, List

# ---------------------------------------------------------------------------
# 国家别名映射：模糊匹配用（用户可能输入本地语言或近似词）
# ---------------------------------------------------------------------------
_ALIASES: Dict[str, str] = {
    # 意大利
    "italian": "italy", "italia": "italy", "it": "italy",
    # 巴西
    "brasileiro": "brazil", "brasil": "brazil", "br": "brazil",
    # 美国
    "american": "usa", "us": "usa", "united states": "usa", "america": "usa",
    # 英国
    "british": "uk", "england": "uk", "united kingdom": "uk", "gb": "uk",
    # 德国
    "german": "germany", "deutschland": "germany", "de": "germany",
    # 法国
    "french": "france", "fr": "france",
    # 西班牙
    "spanish": "spain", "espana": "spain", "españa": "spain", "es": "spain",
    # 印度
    "indian": "india", "in": "india",
    # 沙特
    "saudi": "saudi_arabia", "ksa": "saudi_arabia", "sa": "saudi_arabia",
    # 阿联酋
    "emirati": "uae", "dubai": "uae", "abu dhabi": "uae",
    # 日本
    "japanese": "japan", "jp": "japan",
    # 韩国
    "korean": "south_korea", "korea": "south_korea", "kr": "south_korea",
    # 印尼
    "indonesian": "indonesia", "indo": "indonesia", "id": "indonesia",
    # 墨西哥
    "mexican": "mexico", "mx": "mexico",
    # 阿根廷
    "argentinian": "argentina", "argentine": "argentina", "ar": "argentina",
}

# ---------------------------------------------------------------------------
# 主配置字典
# 每个国家包含: language / edge_tts_voice / timezone / utc_offset /
#              peak_hours / content_style / avoid / trending_platforms /
#              video_aesthetic / music_vibe
# ---------------------------------------------------------------------------
COUNTRY_CONFIGS: Dict[str, Dict[str, Any]] = {

    "italy": {
        "language": "it-IT",
        "edge_tts_voice": "it-IT-DiegoNeural",          # 男声；女声: it-IT-ElsaNeural
        "edge_tts_voice_female": "it-IT-ElsaNeural",
        "timezone": "Europe/Rome",
        "utc_offset": 1,
        "peak_hours": [10, 11, 19, 20, 21],             # UTC，意大利下午茶+晚间黄金时段
        "content_style": "emotional, visual, community-driven, lifestyle-focused",
        "avoid": ["直接销售硬广", "夸张失真的数字承诺", "宗教冒犯"],
        "trending_platforms": ["tiktok", "instagram", "youtube"],
        "video_aesthetic": "warm golden tones, Mediterranean scenery, elegant fashion, artisan food close-ups",
        "music_vibe": "upbeat Italian pop, acoustic guitar, cinematic strings",
    },

    "brazil": {
        "language": "pt-BR",
        "edge_tts_voice": "pt-BR-AntonioNeural",        # 男声；女声: pt-BR-FranciscaNeural
        "edge_tts_voice_female": "pt-BR-FranciscaNeural",
        "timezone": "America/Sao_Paulo",
        "utc_offset": -3,
        "peak_hours": [14, 15, 22, 23, 0],              # UTC，巴西午后+深夜活跃
        "content_style": "energetic, colorful, humor-driven, community celebration",
        "avoid": ["种族刻板印象", "政治敏感话题", "贫富对比内容"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "kwai"],
        "video_aesthetic": "vibrant colors, carnival energy, street culture, tropical backgrounds",
        "music_vibe": "funk carioca, pagode, baile funk, upbeat samba",
    },

    "usa": {
        "language": "en-US",
        "edge_tts_voice": "en-US-GuyNeural",            # 男声；女声: en-US-JennyNeural
        "edge_tts_voice_female": "en-US-JennyNeural",
        "timezone": "America/New_York",
        "utc_offset": -5,
        "peak_hours": [14, 15, 23, 0, 1],               # UTC，美东下午+晚间
        "content_style": "direct, motivational, storytelling, aspirational",
        "avoid": ["政治极端言论", "枪支敏感展示", "种族歧视"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "snapchat"],
        "video_aesthetic": "clean modern aesthetics, diversity showcase, bold typography, fast cuts",
        "music_vibe": "hip-hop beats, pop anthems, lo-fi chill, country crossover",
    },

    "uk": {
        "language": "en-GB",
        "edge_tts_voice": "en-GB-RyanNeural",           # 男声；女声: en-GB-SoniaNeural
        "edge_tts_voice_female": "en-GB-SoniaNeural",
        "timezone": "Europe/London",
        "utc_offset": 0,
        "peak_hours": [12, 13, 19, 20, 21],             # UTC，伦敦午间+晚间
        "content_style": "witty, understated humor, self-deprecating, quality-focused",
        "avoid": ["过度夸张的爱国主义", "皇室争议话题", "阶级刻板印象"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "twitter"],
        "video_aesthetic": "muted tones, urban street style, heritage aesthetics, cozy indoor vibes",
        "music_vibe": "indie pop, grime, Britpop revival, ambient electronic",
    },

    "germany": {
        "language": "de-DE",
        "edge_tts_voice": "de-DE-ConradNeural",         # 男声；女声: de-DE-KatjaNeural
        "edge_tts_voice_female": "de-DE-KatjaNeural",
        "timezone": "Europe/Berlin",
        "utc_offset": 1,
        "peak_hours": [11, 12, 18, 19, 20],             # UTC，德国午间+下班后
        "content_style": "informative, precise, quality-focused, trust-building",
        "avoid": ["夸大宣传", "历史敏感符号", "非正式的廉价感"],
        "trending_platforms": ["instagram", "youtube", "tiktok", "linkedin"],
        "video_aesthetic": "clean minimalist design, engineering precision, natural landscapes, premium product shots",
        "music_vibe": "electronic, techno ambient, classical modern fusion",
    },

    "france": {
        "language": "fr-FR",
        "edge_tts_voice": "fr-FR-HenriNeural",          # 男声；女声: fr-FR-DeniseNeural
        "edge_tts_voice_female": "fr-FR-DeniseNeural",
        "timezone": "Europe/Paris",
        "utc_offset": 1,
        "peak_hours": [11, 12, 19, 20, 21],             # UTC，法国午间+晚间
        "content_style": "chic, sophisticated, intellectual, artistic",
        "avoid": ["美式直销风格", "文化挪用", "低俗幽默"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "snapchat"],
        "video_aesthetic": "Parisian café culture, haute couture aesthetics, artistic black & white, soft film grain",
        "music_vibe": "French jazz, chanson, nu-disco, indie electronic",
    },

    "spain": {
        "language": "es-ES",
        "edge_tts_voice": "es-ES-AlvaroNeural",         # 男声；女声: es-ES-ElviraNeural
        "edge_tts_voice_female": "es-ES-ElviraNeural",
        "timezone": "Europe/Madrid",
        "utc_offset": 1,
        "peak_hours": [12, 13, 21, 22, 23],             # UTC，西班牙午间+深夜活跃
        "content_style": "passionate, festive, family-oriented, food-centric",
        "avoid": ["地区分裂话题（加泰罗尼亚）", "斗牛动物保护争议", "宗教冒犯"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "twitter"],
        "video_aesthetic": "vibrant fiesta colors, tapas culture, flamenco energy, sunny Mediterranean",
        "music_vibe": "reggaeton, flamenco fusion, Latin pop, electronic dance",
    },

    "india": {
        "language": "hi-IN",
        "edge_tts_voice": "hi-IN-MadhurNeural",         # 男声；女声: hi-IN-SwaraNeural
        "edge_tts_voice_female": "hi-IN-SwaraNeural",
        "timezone": "Asia/Kolkata",
        "utc_offset": 5,                                 # +5:30，取整
        "peak_hours": [6, 7, 14, 15, 16],               # UTC，印度早晨+午后
        "content_style": "aspirational, family values, Bollywood-influenced, value-for-money",
        "avoid": ["宗教对立（印度教/伊斯兰）", "种姓话题", "牛相关冒犯内容"],
        "trending_platforms": ["instagram", "youtube", "moj", "tiktok"],
        "video_aesthetic": "colorful festivals, family gatherings, tech-savvy youth culture, street food vibrancy",
        "music_vibe": "Bollywood beats, bhangra fusion, lo-fi Desi, indie Hindi pop",
    },

    "saudi_arabia": {
        "language": "ar-SA",
        "edge_tts_voice": "ar-SA-HamedNeural",          # 男声；女声: ar-SA-ZariyahNeural
        "edge_tts_voice_female": "ar-SA-ZariyahNeural",
        "timezone": "Asia/Riyadh",
        "utc_offset": 3,
        "peak_hours": [8, 9, 17, 18, 19],               # UTC，利雅得晚间（UTC+3）
        "content_style": "aspirational, luxury-focused, Vision 2030 aligned, modest",
        "avoid": ["酒精/猪肉内容", "暴露性别展示", "以色列相关内容", "宗教批评"],
        "trending_platforms": ["tiktok", "instagram", "snapchat", "youtube"],
        "video_aesthetic": "modern Saudi architecture, desert luxury, traditional calligraphy, futuristic NEOM vibes",
        "music_vibe": "Arabic pop, Khaleeji, instrumental oud, ambient Middle Eastern",
    },

    "uae": {
        "language": "ar-AE",
        "edge_tts_voice": "ar-AE-HamdanNeural",         # 男声；女声: ar-AE-FatimaNeural
        "edge_tts_voice_female": "ar-AE-FatimaNeural",
        "timezone": "Asia/Dubai",
        "utc_offset": 4,
        "peak_hours": [7, 8, 16, 17, 18],               # UTC，迪拜晚间（UTC+4）
        "content_style": "luxury, multicultural, innovation-focused, aspirational expat lifestyle",
        "avoid": ["宗教批评", "政治批评", "LGBTQ+内容", "酒精软色情"],
        "trending_platforms": ["instagram", "tiktok", "snapchat", "youtube"],
        "video_aesthetic": "Dubai skyline, luxury supercars, gold aesthetics, futuristic architecture, desert safari",
        "music_vibe": "deep house, Arabic electronic, luxury lounge, international pop",
    },

    "japan": {
        "language": "ja-JP",
        "edge_tts_voice": "ja-JP-KeitaNeural",          # 男声；女声: ja-JP-NanamiNeural
        "edge_tts_voice_female": "ja-JP-NanamiNeural",
        "timezone": "Asia/Tokyo",
        "utc_offset": 9,
        "peak_hours": [0, 1, 9, 10, 11],                # UTC，日本晚间（UTC+9）
        "content_style": "kawaii, precision, detail-obsessed, seasonal, humble storytelling",
        "avoid": ["直接对比竞争对手", "强硬销售话术", "政治历史争议（二战）"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "twitter"],
        "video_aesthetic": "cherry blossoms, anime-inspired, minimalist zen, neon Tokyo streets, seasonal motifs",
        "music_vibe": "city pop revival, J-pop, lo-fi Shibuya, electronic ambient",
    },

    "south_korea": {
        "language": "ko-KR",
        "edge_tts_voice": "ko-KR-InJoonNeural",         # 男声；女声: ko-KR-SunHiNeural
        "edge_tts_voice_female": "ko-KR-SunHiNeural",
        "timezone": "Asia/Seoul",
        "utc_offset": 9,
        "peak_hours": [0, 1, 10, 11, 12],               # UTC，首尔晚间（UTC+9）
        "content_style": "K-culture driven, trendy, beauty-focused, group dynamic, high production value",
        "avoid": ["北韩敏感政治", "日本殖民历史", "丑化韩国传统"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "naver"],
        "video_aesthetic": "K-drama aesthetics, pastel tones, idol-style beauty, street fashion Hongdae",
        "music_vibe": "K-pop, K-R&B, indie Korean, electronic dance",
    },

    "indonesia": {
        "language": "id-ID",
        "edge_tts_voice": "id-ID-ArdiNeural",           # 男声；女声: id-ID-GadisNeural
        "edge_tts_voice_female": "id-ID-GadisNeural",
        "timezone": "Asia/Jakarta",
        "utc_offset": 7,
        "peak_hours": [2, 3, 11, 12, 13],               # UTC，雅加达晚间（UTC+7）
        "content_style": "community-first, religious respect, humor-friendly, value-driven",
        "avoid": ["伊斯兰教批评", "猪肉/酒精内容", "政治批评政府", "种族族裔挑拨"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "shopee"],
        "video_aesthetic": "lush tropical greenery, batik patterns, street food culture, kampung warmth",
        "music_vibe": "dangdut modern, indie pop Indonesia, acoustic Melayu, EDM local",
    },

    "mexico": {
        "language": "es-MX",
        "edge_tts_voice": "es-MX-JorgeNeural",          # 男声；女声: es-MX-DaliaNeural
        "edge_tts_voice_female": "es-MX-DaliaNeural",
        "timezone": "America/Mexico_City",
        "utc_offset": -6,
        "peak_hours": [15, 16, 0, 1, 2],                # UTC，墨西哥晚间（UTC-6）
        "content_style": "family-centric, vibrant humor, food passion, DIY resourcefulness",
        "avoid": ["毒品卡特尔话题", "移民政治敏感", "贫富强烈对比"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "facebook"],
        "video_aesthetic": "Día de Muertos colorful skulls, mariachi culture, street tacos, colonial architecture",
        "music_vibe": "regional Mexican, cumbia, Latin trap, banda sinfónica",
    },

    "argentina": {
        "language": "es-AR",
        "edge_tts_voice": "es-AR-TomasNeural",          # 男声；女声: es-AR-ElenaNeural
        "edge_tts_voice_female": "es-AR-ElenaNeural",
        "timezone": "America/Argentina/Buenos_Aires",
        "utc_offset": -3,
        "peak_hours": [14, 15, 22, 23, 0],              # UTC，布宜诺斯艾利斯晚间（UTC-3）
        "content_style": "passionate, football-obsessed, tango cultural pride, intellectual debate",
        "avoid": ["马岛主权挑衅", "经济危机刻板印象", "贬低文化认同"],
        "trending_platforms": ["tiktok", "instagram", "youtube", "twitter"],
        "video_aesthetic": "Buenos Aires urban chic, tango drama, Patagonia landscapes, football stadium energy",
        "music_vibe": "tango electrónico, cumbia villera, rock nacional, reggaeton Río de la Plata",
    },
}


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def get_country_config(country: str) -> Dict[str, Any]:
    """
    按国家名称获取内容策略配置，支持模糊匹配。
    - "italian" -> "italy"
    - "Brasil"  -> "brazil"
    若找不到则返回美国默认配置并附加 warning 键。

    参数:
        country: 国家名称（英文，大小写不敏感）

    返回:
        配置 dict，额外附加 "country_key" 字段标记实际匹配到的国家。
    """
    key = country.strip().lower().replace("-", "_").replace(" ", "_")

    # 直接匹配
    if key in COUNTRY_CONFIGS:
        cfg = dict(COUNTRY_CONFIGS[key])
        cfg["country_key"] = key
        return cfg

    # 别名匹配
    resolved = _ALIASES.get(key)
    if resolved and resolved in COUNTRY_CONFIGS:
        cfg = dict(COUNTRY_CONFIGS[resolved])
        cfg["country_key"] = resolved
        return cfg

    # 部分包含匹配（兜底）
    for canonical in COUNTRY_CONFIGS:
        if canonical in key or key in canonical:
            cfg = dict(COUNTRY_CONFIGS[canonical])
            cfg["country_key"] = canonical
            return cfg

    # 找不到，返回美国默认配置
    cfg = dict(COUNTRY_CONFIGS["usa"])
    cfg["country_key"] = "usa"
    cfg["warning"] = f"未找到国家 '{country}'，已回退至默认配置 (usa)"
    return cfg


def get_tts_voice(country: str, gender: str = "neutral") -> str:
    """
    获取指定国家的 Edge-TTS 语音名称。

    参数:
        country: 国家名称
        gender:  "male" / "female" / "neutral"（neutral 默认返回男声）

    返回:
        Edge-TTS voice 字符串，如 "it-IT-DiegoNeural"
    """
    cfg = get_country_config(country)
    if gender == "female":
        return cfg.get("edge_tts_voice_female", cfg["edge_tts_voice"])
    return cfg["edge_tts_voice"]


def list_supported_countries() -> List[str]:
    """
    返回所有已配置国家的 canonical key 列表，字母排序。
    """
    return sorted(COUNTRY_CONFIGS.keys())


def get_peak_utc_hours(country: str) -> List[int]:
    """
    返回指定国家的最佳发帖时间（UTC 小时列表）。

    参数:
        country: 国家名称

    返回:
        List[int]，如 [10, 11, 19, 20, 21]
    """
    cfg = get_country_config(country)
    return cfg.get("peak_hours", [12, 18, 20])
