from .base_publisher import BasePublisher, PublishResult
from .tiktok_publisher import TikTokPublisher
from .instagram_publisher import InstagramPublisher
from .telegram_publisher import TelegramPublisher
from .twitter_publisher import TwitterPublisher
from .facebook_publisher import FacebookPublisher
from .linkedin_publisher import LinkedInPublisher
from .whatsapp_publisher import WhatsAppPublisher
from .xiaohongshu_publisher import XiaohongshuPublisher

_PUBLISHERS = {
    "tiktok":       TikTokPublisher,
    "instagram":    InstagramPublisher,
    "telegram":     TelegramPublisher,
    "twitter":      TwitterPublisher,
    "x":            TwitterPublisher,
    "facebook":     FacebookPublisher,
    "fb":           FacebookPublisher,
    "linkedin":     LinkedInPublisher,
    "whatsapp":     WhatsAppPublisher,
    "wa":           WhatsAppPublisher,
    "xiaohongshu":  XiaohongshuPublisher,
    "xhs":          XiaohongshuPublisher,
    "redbook":      XiaohongshuPublisher,
}


def get_publisher(platform: str, device_id: str = "", **kwargs):
    """工厂函数：根据平台名返回对应的发布器实例。

    Args:
        platform: 平台名（tiktok/instagram/telegram/twitter/x/facebook/fb/linkedin/whatsapp/wa）
        device_id: ADB设备ID（可选，默认使用第一个已连接设备）
        **kwargs: 传递给发布器构造函数的额外参数

    Returns:
        BasePublisher 实例
    """
    cls = _PUBLISHERS.get(platform.lower())
    if not cls:
        raise ValueError(
            f"不支持的平台: {platform}. 支持的平台: {list(_PUBLISHERS.keys())}"
        )
    if device_id:
        return cls(device_id=device_id, **kwargs)
    return cls(**kwargs)


def list_platforms() -> list:
    """返回所有支持的平台名称（去重）。"""
    seen = set()
    result = []
    for k, v in _PUBLISHERS.items():
        if v not in seen:
            seen.add(v)
            result.append(k)
    return result
