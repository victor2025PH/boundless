// Single source of truth for brand identity (无界科技 BOUNDLESS).
// Change names / taglines / products here — the rest of the app imports from this file.

// 三系（category）：母品牌无界统领，每系破一「界」。产品归系见各产品 category 字段。
export const CATEGORIES = {
  growth: { zh: "智连", en: "Growth", breakZh: "沟通与成交之界", breakEn: "the reach & sales barrier" },
  studio: { zh: "幻境", en: "Studio", breakZh: "容貌 / 声音 / 身份之界", breakEn: "the face / voice / identity barrier" },
  lingo: { zh: "通达", en: "Lingo", breakZh: "语言之界", breakEn: "the language barrier" },
} as const;

export type CategoryKey = keyof typeof CATEGORIES;
export const CATEGORY_ORDER: CategoryKey[] = ["growth", "studio", "lingo"];

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
      break: { zh: "触达与获客之界", en: "the reach barrier" },
      desc: {
        zh: "真机多号自动加友、打招呼、群提取，7×24 全自动获客引流进私域",
        en: "Multi-device auto add / greet / group-extract — automated lead-gen into your funnel",
      },
      // 获客能力对应 mobile-auto0423（OpenClaw 真机 RPA 集群）；非 solutions SKU。
      skuIds: [],
    },
    chatx: {
      category: "growth",
      zh: "智聊",
      en: "ChatX",
      alt: "ChatHub",
      emoji: "💬",
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
      break: { zh: "语言之界（口译）", en: "the interpreting barrier" },
      desc: {
        zh: "会议 / 直播实时语音同传：克隆音同传 + 双语字幕 + 抢话打断",
        en: "Real-time voice interpreting: cloned-voice simul-interpret + subtitles",
      },
      skuIds: [],
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

/** 七产品的固定展示顺序，按三系分组：智连(智拓·智聊) → 幻境(幻颜·幻声·幻影) → 通达(通译·通传)。 */
export const PRODUCT_ORDER: ProductKey[] = ["reachx", "chatx", "facex", "voicex", "livex", "lingox", "voxx"];

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
