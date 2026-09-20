"""FateX（问衍）—— 命理陪伴独立产品层。

与主产品（多平台 AI 陪伴）**同底座、不同产品**：
- 共享：多平台接入（TG/LINE/Messenger/WA）、AI/语音管线、权益（entitlement）、观测基建；
- 独立：产品品牌（FateX / 问衍）、**独立数据库**（``fatex.db``：结构化生辰画像，
  见 :mod:`src.fatex.store`）、配置命名空间（顶层 ``fatex.*``，兼容旧
  ``companion.bazi.*``，见 :mod:`src.fatex.config`）。

算法引擎（排盘/灵签/K 线渲染，``src/companion/bazi_*.py``）属**共享底座算法库**，
FateX 是其产品化外壳：数据归 FateX、体验归 FateX、品牌归 FateX；
主产品仅经产品边界 API 调用，两边数据互不落对方库。

品牌资产：``brand-assets/02_product-icons/fatex/``（与 matrixx/voicex 家族同管线生成）。
"""
from __future__ import annotations

PRODUCT_ID = "fatex"
PRODUCT_NAME = "FateX"
# 家族命名律：智X=增长系 / 幻X=幻境系 / 通X=翻译系 —— 命理归幻境系，
# 「幻缘」＝缘分 × 命运 × 情感陪伴（与幻颜/幻声/幻影同族）。
PRODUCT_NAME_CN = "幻缘"
PRODUCT_TAGLINE_CN = "知缘知运 · 人生 K 线"
PRODUCT_TAGLINE_EN = "Ask fate, chart life."
PRODUCT_VERSION = "1.0.0"


def product_badge() -> str:
    """观测/日志用的产品标识（ops 卡、metrics 标签统一取这里）。"""
    return f"{PRODUCT_NAME} {PRODUCT_NAME_CN}"
