// Single source of truth for brand identity (无界科技 BOUNDLESS).
// Change names / taglines / products here — the rest of the app imports from this file.

// 三系（category）：母品牌无界统领，每系破一「界」。产品归系见各产品 category 字段。
// accent 与 brand-assets 头像光环色同源：智连蓝 / 幻境紫 / 通达橙。
export const CATEGORIES = {
  growth: {
    zh: "智连",
    en: "Growth",
    breakZh: "沟通与成交之界",
    breakEn: "the reach & sales barrier",
    accent: "cyan" as const,
  },
  studio: {
    zh: "幻境",
    en: "Studio",
    breakZh: "容貌 / 声音 / 身份之界",
    breakEn: "the face / voice / identity barrier",
    accent: "violet" as const,
  },
  lingo: {
    zh: "通达",
    en: "Lingo",
    breakZh: "语言之界",
    breakEn: "the language barrier",
    accent: "amber" as const,
  },
} as const;

export type CategoryKey = keyof typeof CATEGORIES;
// 陈列顺序 = 商业主线：通达(通译 SCRM·低风险现金流) → 智连(获客护城河) → 幻境(换脸/直播降为定制，殿后)。
// 驱动导航下拉 / 产品矩阵 / 品牌页 / 展示墙的统一顺序（各处仅按此 map，不假设某系在首位）。
export const CATEGORY_ORDER: CategoryKey[] = ["lingo", "growth", "studio"];

export const BRAND = {
  company: {
    zh: "无界科技",
    en: "BOUNDLESS",
    full: "无界科技 BOUNDLESS",
    logoChar: "界",
    tagline: {
      zh: "让沟通，无界",
      en: "Communication, Boundless.",
    },
  },
  // 三系七产品（无界品牌族）：每个打破一种「界」，每个产品用 category 归系。
  // 英文主名走统一 `…X` 系列（X = 突破边界 / 无限变换）；alt 为更自解释的渠道备选名。
  products: {
    reachx: {
      category: "growth",
      zh: "智拓",
      en: "ReachX",
      alt: "GrowthReach",
      emoji: "🎯",
      scene: { zh: "真机获客", en: "Lead gen" },
      break: { zh: "触达与获客之界", en: "the reach barrier" },
      desc: {
        zh: "真机多号自动加友、打招呼、群提取，7×24 全自动获客引流进私域",
        en: "Multi-device auto add / greet / group-extract — automated lead-gen into your funnel",
      },
      // 获客能力对应 mobile-auto0423（OpenClaw 真机 RPA 集群）；SKU 卡 = content.solutions#reach。
      skuIds: ["reach"],
    },
    chatx: {
      category: "growth",
      zh: "智聊",
      en: "ChatX",
      alt: "ChatHub",
      emoji: "💬",
      scene: { zh: "AI 成交", en: "AI closing" },
      break: { zh: "沟通与成交之界", en: "the sales barrier" },
      desc: {
        zh: "聚合 AI 聊天：全程自动开发客户、推进成交",
        en: "Omni-channel AI chat that closes deals",
      },
      // 智聊能力在 content.ts::autochat / plans，不在 solutions SKU 列表中。
      skuIds: [],
    },
    facex: {
      category: "studio",
      zh: "幻颜",
      en: "FaceX",
      alt: "FaceSwap",
      emoji: "🎭",
      scene: { zh: "AI 换脸", en: "Face swap" },
      break: { zh: "容貌之界", en: "the face barrier" },
      desc: {
        zh: "AI 换脸：图片 / 视频里随心变幻容貌",
        en: "AI face swap for images & video",
      },
      // 对应 content.ts::solutions 的底层 SKU id（产品↔SKU 映射单一来源）。
      skuIds: ["faceswap"],
    },
    voicex: {
      category: "studio",
      zh: "幻声",
      en: "VoiceX",
      alt: "VoiceClone",
      emoji: "🎙",
      scene: { zh: "声音克隆", en: "Voice clone" },
      break: { zh: "声音之界", en: "the voice barrier" },
      desc: {
        zh: "AI 声音克隆：惟妙惟肖的配音与语音合成",
        en: "Clone any voice for lifelike dubbing",
      },
      skuIds: ["voice"],
    },
    livex: {
      category: "studio",
      zh: "幻影",
      en: "LiveX",
      alt: "LiveMorph",
      emoji: "🎬",
      scene: { zh: "直播分身", en: "Live twin" },
      break: { zh: "身份之界", en: "the identity barrier" },
      desc: {
        zh: "实时直播换脸换声：低延迟的百变分身",
        en: "Real-time face & voice swap for live",
      },
      skuIds: ["digital-human", "video-dubbing"],
    },
    lingox: {
      category: "lingo",
      zh: "通译",
      en: "LingoX",
      alt: "LiveLingo",
      emoji: "🌐",
      scene: { zh: "聊天翻译", en: "Chat translate" },
      break: { zh: "语言之界（聊天）", en: "the language barrier" },
      desc: {
        zh: "实时聊天翻译：多平台文字 + 语音双向互译",
        en: "Real-time chat translation across platforms",
      },
      skuIds: ["translate"],
    },
    voxx: {
      category: "lingo",
      zh: "通传",
      en: "VoxX",
      alt: "LiveInterpret",
      emoji: "🎧",
      scene: { zh: "同声传译", en: "Interpreting" },
      break: { zh: "语言之界（口译）", en: "the interpreting barrier" },
      desc: {
        zh: "会议 / 直播实时语音同传：克隆音同传 + 双语字幕 + 抢话打断",
        en: "Real-time voice interpreting: cloned-voice simul-interpret + subtitles",
      },
      skuIds: ["interpret"],
    },
  },
  engine: {
    zh: "无界底座",
    en: "BOUNDLESS Engine",
  },
  // 解锁 / 折扣码前缀
  discountPrefix: "BL",
  // 语言偏好的 localStorage key（沿用旧 key，避免老用户语言偏好丢失）
  langStorageKey: "hl-lang",
} as const;

export type BrandLang = "zh" | "en";
export type ProductKey = keyof typeof BRAND.products;

/** 七产品的固定展示顺序，与 CATEGORY_ORDER 对齐（商业主线）：
 *  通达(通译·通传) → 智连(智拓·智聊) → 幻境(幻声·幻颜·幻影)。
 *  通译领跑现金流、幻声(低风险第二现金流)在幻境系居首、换脸/直播殿后。
 *  驱动矩阵卡编号 / bot 与 SEO 的产品概述顺序，故与 CATEGORY_ORDER 保持一致。 */
export const PRODUCT_ORDER: ProductKey[] = ["lingox", "voxx", "reachx", "chatx", "voicex", "facex", "livex"];

/** 产品数量唯一真相——UI / bot / SEO 禁止手写「六大/七条」数字，统一拼此常量。 */
export const PRODUCT_COUNT = PRODUCT_ORDER.length;

/**
 * 品牌家族口径（七款产品 · 破六道边界）。
 * 通译与通传同破「语言之界」，所以产品数 7、边界数 6——这是刻意的记忆点，不是笔误。
 */
export const FAMILY_PITCH = {
  zh: {
    headline: "一个无界底座 · 七款产品 · 破六道边界",
    sub: "同一套私有化底座，打破触达、成交、容貌、声音、身份、语言六道边界——七款产品按需单选，或组合成从获客到成交的完整闭环。",
  },
  en: {
    headline: "One core · Seven products · Six barriers broken",
    sub: "One private-deployment core breaks the barriers of reach, closing, face, voice, identity and language — pick any line, or combine them into a full loop from lead-gen to close.",
  },
} as const;

/** 某系下的产品（按 PRODUCT_ORDER 顺序）。ProductMatrix / 导航按系陈列时消费。 */
export function productsInCategory(cat: CategoryKey): ProductKey[] {
  return PRODUCT_ORDER.filter((k) => BRAND.products[k].category === cat);
}

/** "无界科技 BOUNDLESS" 这类中英组合写法。 */
export function brandFull(): string {
  return BRAND.company.full;
}

/** 产品中英组合：幻颜 FaceX / 智聊 ChatX。 */
export function productLabel(key: ProductKey, lang: BrandLang = "zh"): string {
  const p = BRAND.products[key];
  return lang === "zh" ? `${p.zh} ${p.en}` : `${p.en} (${p.zh})`;
}

/** 七产品的结构化清单（emoji + 名称 + 一句话能力），按固定展示顺序。
 *  欢迎语 / bot 知识库 / system prompt / 营销帖等"产品线概述"统一消费这一份，
 *  避免同一段产品介绍散落多个文件、改一处漏五处。 */
export function productLineItems(lang: BrandLang) {
  return PRODUCT_ORDER.map((k) => {
    const p = BRAND.products[k];
    return {
      key: k,
      emoji: p.emoji,
      name: productLabel(k, lang),
      desc: p.desc[lang],
    };
  });
}

/** 产品概述的纯文本块（每行 "· 🎭 幻颜 FaceX：AI 换脸…"），用于 bot 文案拼接。
 *  bullet 默认 "· "，html=true 时名称用 <b> 包裹（Telegram HTML parse_mode）。 */
export function productLinesText(lang: BrandLang, opts?: { bullet?: string; html?: boolean }): string {
  const bullet = opts?.bullet ?? "· ";
  const sep = lang === "zh" ? "：" : ": ";
  return productLineItems(lang)
    .map((it) => {
      const name = opts?.html ? `<b>${it.name}</b>` : it.name;
      return `${bullet}${it.emoji} ${name}${sep}${it.desc}`;
    })
    .join("\n");
}
