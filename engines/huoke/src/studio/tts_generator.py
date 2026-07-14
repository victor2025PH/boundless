# -*- coding: utf-8 -*-
"""
TTS 配音生成器 — 使用 edge-tts（微软免费TTS，50+语言）。

完全免费，无需API Key，支持50+语言和声音。
生成的音频文件保存到 data/studio/audio/ 目录。
"""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import List, Optional

import edge_tts

from src.host.device_registry import data_dir

logger = logging.getLogger(__name__)

# 音频输出目录（项目根 data/studio/audio）
AUDIO_DIR = data_dir() / "studio" / "audio"

# 国家/语言 → 最佳 edge-tts 声音映射表
# 格式: { key: {"female": voice, "male": voice} }
VOICE_MAP: dict = {
    # 意大利语
    "italy":       {"female": "it-IT-ElsaNeural",       "male": "it-IT-DiegoNeural"},
    "italian":     {"female": "it-IT-ElsaNeural",       "male": "it-IT-DiegoNeural"},
    # 葡萄牙语 / 巴西
    "brazil":      {"female": "pt-BR-FranciscaNeural",  "male": "pt-BR-AntonioNeural"},
    "portuguese":  {"female": "pt-BR-FranciscaNeural",  "male": "pt-BR-AntonioNeural"},
    # 英语
    "english":     {"female": "en-US-JennyNeural",      "male": "en-GB-RyanNeural"},
    "usa":         {"female": "en-US-JennyNeural",      "male": "en-US-GuyNeural"},
    "uk":          {"female": "en-GB-LibbyNeural",      "male": "en-GB-RyanNeural"},
    "india":       {"female": "en-IN-NeerjaNeural",     "male": "en-IN-PrabhatNeural"},
    # 阿拉伯语
    "arabic":      {"female": "ar-SA-ZariyahNeural",   "male": "ar-SA-HamedNeural"},
    # 法语
    "french":      {"female": "fr-FR-DeniseNeural",     "male": "fr-FR-HenriNeural"},
    "france":      {"female": "fr-FR-DeniseNeural",     "male": "fr-FR-HenriNeural"},
    # 德语
    "german":      {"female": "de-DE-KatjaNeural",      "male": "de-DE-ConradNeural"},
    "germany":     {"female": "de-DE-KatjaNeural",      "male": "de-DE-ConradNeural"},
    # 西班牙语 / 墨西哥
    "spanish":     {"female": "es-MX-DaliaNeural",      "male": "es-ES-AlvaroNeural"},
    "mexico":      {"female": "es-MX-DaliaNeural",      "male": "es-MX-JorgeNeural"},
    "spain":       {"female": "es-ES-ElviraNeural",     "male": "es-ES-AlvaroNeural"},
    # 日语
    "japanese":    {"female": "ja-JP-NanamiNeural",     "male": "ja-JP-KeitaNeural"},
    "japan":       {"female": "ja-JP-NanamiNeural",     "male": "ja-JP-KeitaNeural"},
    # 韩语
    "korean":      {"female": "ko-KR-SunHiNeural",      "male": "ko-KR-InJoonNeural"},
    "korea":       {"female": "ko-KR-SunHiNeural",      "male": "ko-KR-InJoonNeural"},
    # 中文
    "chinese":     {"female": "zh-CN-XiaoxiaoNeural",  "male": "zh-CN-YunxiNeural"},
    "china":       {"female": "zh-CN-XiaoxiaoNeural",  "male": "zh-CN-YunxiNeural"},
    # 印尼语
    "indonesian":  {"female": "id-ID-GadisNeural",      "male": "id-ID-ArdiNeural"},
    "indonesia":   {"female": "id-ID-GadisNeural",      "male": "id-ID-ArdiNeural"},
    # 印地语
    "hindi":       {"female": "hi-IN-SwaraNeural",      "male": "hi-IN-MadhurNeural"},
}

_DEFAULT_VOICE = "en-US-JennyNeural"


def get_voice_for_country(country: str, gender: str = "female") -> str:
    """根据国家/语言名称和性别返回 edge-tts 声音名称。

    Args:
        country: 国家或语言名称（大小写不敏感），如 "italy"、"chinese"
        gender:  "female"（默认）或 "male"

    Returns:
        edge-tts 声音名称字符串，找不到时返回默认英语女声。
    """
    key = country.lower().strip()
    voices = VOICE_MAP.get(key)
    if not voices:
        logger.warning("未找到 '%s' 的声音配置，使用默认英语女声", country)
        return _DEFAULT_VOICE
    gender_key = "female" if gender.lower() != "male" else "male"
    return voices.get(gender_key, _DEFAULT_VOICE)


async def generate_voiceover(
    text: str,
    country: str,
    gender: str = "female",
    output_path: Optional[str] = None,
    rate: str = "+0%",
    volume: str = "+0%",
) -> str:
    """异步生成配音音频文件并保存为 MP3。

    Args:
        text:        要转换为语音的文字内容
        country:     目标国家/语言，如 "chinese"、"italy"、"usa"
        gender:      声音性别，"female"（默认）或 "male"
        output_path: 可选，指定输出文件路径；不填则自动生成 UUID 文件名
        rate:        语速调整，如 "+10%"、"-5%"，默认 "+0%"
        volume:      音量调整，如 "+10%"、"-5%"，默认 "+0%"

    Returns:
        生成的 MP3 文件绝对路径字符串。

    Raises:
        RuntimeError: 当 edge-tts 调用失败时抛出。
    """
    voice = get_voice_for_country(country, gender)
    logger.info("TTS生成 | 声音=%s | 速率=%s | 文本长度=%d字", voice, rate, len(text))

    if output_path:
        dest = Path(output_path)
    else:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        dest = AUDIO_DIR / f"{uuid.uuid4()}.mp3"

    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, volume=volume)
        await communicate.save(str(dest))
        logger.info("TTS完成 → %s", dest)
        return str(dest)
    except Exception as exc:
        logger.error("TTS生成失败: %s", exc)
        raise RuntimeError(f"edge-tts 生成失败: {exc}") from exc


def generate_voiceover_sync(
    text: str,
    country: str,
    gender: str = "female",
    output_path: Optional[str] = None,
    rate: str = "+0%",
    volume: str = "+0%",
) -> str:
    """同步包装器 — 直接调用 generate_voiceover 的同步版本。

    在不支持 async/await 的环境中使用此函数。
    """
    return asyncio.run(
        generate_voiceover(text, country, gender, output_path, rate, volume)
    )


async def list_voices_for_language(language_code: str) -> List[dict]:
    """列出指定语言代码的所有可用 edge-tts 声音。

    Args:
        language_code: BCP-47 语言代码，如 "zh-CN"、"en-US"、"ja-JP"

    Returns:
        包含声音信息的字典列表，每项包含 Name、Gender、Locale 等字段。
    """
    try:
        all_voices = await edge_tts.list_voices()
        prefix = language_code.lower()
        matched = [v for v in all_voices if v.get("Locale", "").lower().startswith(prefix)]
        logger.info("语言 %s 找到 %d 个声音", language_code, len(matched))
        return matched
    except Exception as exc:
        logger.error("获取声音列表失败: %s", exc)
        return []
