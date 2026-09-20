// 幻境 STUDIO 客户端（原 AvatarHub；会员订阅 / 私有授权）定价单一真相 —
// /order 购买面板与 /download 下载页共用。与引擎授权档（trial/standard/pro/enterprise）一一对应。
// 商业模式：我们不提供算力——设备用户自备（可代选配），我们协助部署；
// 引擎在用户本机运行，因此不按字符/张数/时长计费，档位差异只在能力、画质、并发与服务。
// 2026-08-04 定价改版：免费版（换脸+水印）/ 入门 39 / 标准 199 / 专业 499（月付），
// 季付、年付为显式挂牌价（非公式推导）；旗舰版改为私有部署定制、咨询客服报价。
// 改价只改这里；lib/pricing.ts 是官网服务类 SKU（保持不动，两套并存）。

export type Period = "monthly" | "quarterly" | "annual";

/** 无显式挂牌价的产品线（ChatX / LingoX 适配层）按月价推导：季付 ×3、年付 ×10（送 2 个月）。 */
export const QUARTER_MONTHS = 3;
export const ANNUAL_MONTHS = 10;

/** TRC20 收款地址：在 .env.local 配 NEXT_PUBLIC_USDT_ADDR；未配置时页面引导找客服取地址（防呆）。 */
export const USDT_ADDR = process.env.NEXT_PUBLIC_USDT_ADDR || "";

export interface Tier {
  key: string;
  edition: "trial" | "standard" | "pro" | "enterprise";
  monthly: number; // 0 = 免费档 / 定制档；挂牌币种 USD（2026-07-18 全站报价币种统一，链上结算仍走 USDT）
  /** 显式季付 / 年付挂牌价（幻境 STUDIO 五档用）；缺省时按 monthly 推导（×3 / ×10）。 */
  quarterly?: number;
  annual?: number;
  /** 定制报价档（旗舰）：不挂牌上架价，CTA 引导咨询客服获取报价方案。 */
  custom?: boolean;
  hot?: boolean;
  name: { zh: string; en: string };
  audience: { zh: string; en: string };
  feats: { zh: string[]; en: string[] };
  /** 一句话档位摘要：studioTierRows / JSON-LD offers 等紧凑展示位共用（feats 太长塞不下）。
   *  可选——order-lines.ts 的 ChatX/LingoX 适配层 Tier 不需要；STUDIO 五档必须填
   *  （派生函数缺 blurb 时回落 audience）。 */
  blurb?: { zh: string; en: string };
}

export const TIERS: Tier[] = [
  {
    key: "trial",
    edition: "trial",
    monthly: 0,
    name: { zh: "免费版 Free", en: "Free" },
    audience: { zh: "下载即用 · 免费换脸", en: "Free forever · face swap" },
    feats: {
      zh: ["图片 / 视频换脸免费用", "输出带合规水印", "本机算力 · 用量不限", "1 路会话"],
      en: ["Free photo / video face swap", "Compliance watermark on output", "Your hardware · unlimited usage", "1 session"],
    },
    blurb: { zh: "换脸 + 水印 · 本机算力", en: "Face swap + watermark · your hardware" },
  },
  {
    key: "starter",
    edition: "standard",
    monthly: 39,
    quarterly: 119,
    annual: 359,
    name: { zh: "入门版 Starter", en: "Starter" },
    audience: { zh: "个人 / 起步 · 单机", en: "Personal · single machine" },
    feats: {
      zh: ["AI 作图 · 图片换脸不限量", "视频换脸不限量", "去水印输出", "社区支持"],
      en: ["AI image gen · unlimited photo swap", "Unlimited video face swap", "No watermark", "Community support"],
    },
    blurb: { zh: "作图 · 图片 / 视频换脸", en: "Image gen · photo/video swap" },
  },
  {
    key: "standard",
    edition: "standard",
    monthly: 199,
    quarterly: 399,
    annual: 1199,
    name: { zh: "标准版 Standard", en: "Standard" },
    audience: { zh: "主播 / 内容创作者", en: "Streamers / creators" },
    feats: {
      zh: ["入门版全部能力", "直播实时换脸 · 虚拟摄像头", "实时变声器", "工单支持"],
      en: ["Everything in Starter", "Live face swap · virtual camera", "Real-time voice changer", "Ticket support"],
    },
    blurb: { zh: "直播换脸 · 变声器", en: "Live swap · voice changer" },
  },
  {
    key: "pro",
    edition: "pro",
    monthly: 499,
    quarterly: 999,
    annual: 2999,
    hot: true,
    name: { zh: "专业版 Pro", en: "Pro" },
    audience: { zh: "工作室 / MCN", en: "Studios / MCN" },
    feats: {
      zh: ["标准版全部能力", "直播换脸 + 同声传译", "克隆音同传 · 多语直播", "优先支持"],
      en: ["Everything in Standard", "Live face swap + interpreting", "Cloned-voice interpreting · multilingual streams", "Priority support"],
    },
    blurb: { zh: "直播换脸 · 同声传译", en: "Live swap · interpreting" },
  },
  {
    key: "flagship",
    edition: "enterprise",
    monthly: 0,
    custom: true,
    name: { zh: "旗舰版 Flagship", en: "Flagship" },
    audience: { zh: "企业级 · 私有化", en: "Enterprise · private" },
    feats: {
      zh: ["专业版全部能力", "私有化部署 · 定制开发", "所有专业私域服务", "专属对接 · 远程调优"],
      en: ["Everything in Pro", "Private deployment · custom dev", "Full pro private-domain services", "Dedicated support · remote tuning"],
    },
    blurb: { zh: "私有部署定制 · 专业私域服务", en: "Private deployment · custom services" },
  },
];

/* ── 展示层派生（2026-08-04 幻境报价单源化）─────────────────────────────────
 * content.ts 里幻声 / 幻影 / 视频配音等营销卡的五档报价行、layout.tsx 的 JSON-LD
 * offers 一律由下面的派生函数取数——此前五档在 content.ts 中英共硬编码 6 份、
 * JSON-LD 还挂着 voiceOffers 旧价（18/78/198），改价必漏。改价只改上方 TIERS。 */

/** 档位短名：zh 取「免费版 Free」的中文段，en 用英文名。 */
export function tierShortName(t: Tier, lang: "zh" | "en"): string {
  return lang === "zh" ? t.name.zh.split(" ")[0] : t.name.en;
}

/** 挂牌价标签：免费 / 咨询报价 / N / 月。 */
export function tierPriceLabel(t: Tier, lang: "zh" | "en"): string {
  if (t.custom) return lang === "zh" ? "咨询报价" : "Quote";
  if (t.monthly === 0) return lang === "zh" ? "免费" : "Free";
  return lang === "zh" ? `${t.monthly} / 月` : `${t.monthly} / mo`;
}

/** 营销卡报价行（content.ts Solution.pricing 同构）：五档全量，detail=blurb+季付/年付挂牌。 */
export function studioTierRows(lang: "zh" | "en"): { plan: string; price: string; detail: string; order: string }[] {
  return TIERS.map((t) => {
    const qa =
      t.quarterly && t.annual
        ? lang === "zh"
          ? ` · 季付 ${t.quarterly} · 年付 ${t.annual}`
          : ` · Q ${t.quarterly} · Y ${t.annual}`
        : "";
    return {
      plan: tierShortName(t, lang),
      price: tierPriceLabel(t, lang),
      detail: `${(t.blurb ?? t.audience)[lang]}${qa}`,
      order: t.key,
    };
  });
}

/** 单档报价行（video-dubbing 这类只引用部分档位、detail 需自定义文案的卡用它取价）。 */
export function studioTier(key: string): Tier {
  const t = TIERS.find((x) => x.key === key);
  if (!t) throw new Error(`avatarhub-pricing: unknown tier ${key}`);
  return t;
}

/** 付费档起价（“39 USD/月起”这类文案的数字来源，勿再手写）。 */
export const STUDIO_PAID_FROM = Math.min(...TIERS.filter((t) => !t.custom && t.monthly > 0).map((t) => t.monthly));

/** JSON-LD offers（schema.org Offer 源数据）：仅挂牌付费档（免费/定制档不进结构化数据）。
 *  形状对齐 lib/pricing.ts::PriceOffer，layout.tsx 经 toSchemaOffer 消费。 */
export function studioSchemaOffers(): {
  id: string;
  name: string;
  price: string;
  currency: "USD";
  unit: "month";
  description: string;
}[] {
  return TIERS.filter((t) => !t.custom && t.monthly > 0).map((t) => ({
    id: `studio-${t.key}`,
    name: `STUDIO ${t.name.en}`,
    price: String(t.monthly),
    currency: "USD" as const,
    unit: "month" as const,
    description: `Per month; ${(t.blurb ?? t.audience).en}.`,
  }));
}

/* ── 能力 → 档位对照（2026-08-07 加，修「找不到幻影/某产品的价格」实录工单）──
 * 客户/老板带着产品名（幻影/幻声/通传…）来找价格，而档位卡按能力分层不按产品名，
 * 两套词汇对不上 → 在 /order 档位卡下方给一张「我要的能力在哪个档」对照表。
 * 行内容严格对齐上方 TIERS.feats 的原词（那里没有的能力这里不许出现——写错档位
 * 就是又一次报价口径事故）；档位名与价格经 tierShortName/tierPriceLabel 派生，零手写数字。 */
export interface CapabilityRow {
  /** 客户嘴里的需求（用 TIERS.feats 原词） */
  need: { zh: string; en: string };
  /** 对应产品线名（品牌词，帮「拿着产品名找价」的人对上号） */
  product: { zh: string; en: string };
  /** 起始档位 key（studioTier 派生名称与价格） */
  tierKey: string;
  note?: { zh: string; en: string };
}

export const CAPABILITY_TIERS: CapabilityRow[] = [
  {
    need: { zh: "AI 作图 · 图片 / 视频换脸", en: "AI image gen · photo / video face swap" },
    product: { zh: "幻颜 FaceX", en: "FaceX" },
    tierKey: "trial",
    note: { zh: "免费档带水印；入门版起去水印", en: "Free tier watermarked; Starter+ removes it" },
  },
  {
    need: { zh: "直播实时换脸 · 虚拟摄像头", en: "Live face swap · virtual camera" },
    product: { zh: "幻影 LiveX", en: "LiveX" },
    tierKey: "standard",
  },
  {
    need: { zh: "实时变声器", en: "Real-time voice changer" },
    product: { zh: "幻声 VoiceX", en: "VoiceX" },
    tierKey: "standard",
  },
  {
    need: { zh: "克隆音同传 · 多语直播", en: "Cloned-voice interpreting · multilingual streams" },
    product: { zh: "通传 VoxX", en: "VoxX" },
    tierKey: "pro",
  },
  {
    need: { zh: "私有化部署 · 定制开发", en: "Private deployment · custom development" },
    product: { zh: "无界底座", en: "BOUNDLESS Engine" },
    tierKey: "flagship",
  },
];

/** 部署版本 × 最低硬件配置（设备自备；拿不准就买「远程代部署」，我们帮选型）。
 *  依据引擎交付基线：NVIDIA 显卡 + Windows 10/11；显存 Lite ≥8GB / 标准 ≥16GB / 旗舰 ≥24GB；
 *  首次部署按档位下载 11–35GB 模型。 */
export interface HardwareRow {
  tier: { zh: string; en: string };
  gpu: string;
  ram: string;
  disk: string;
  can: { zh: string; en: string };
}

export const HARDWARE: HardwareRow[] = [
  {
    tier: { zh: "免费 / 入门版", en: "Free / Starter" },
    gpu: "RTX 3060 12GB / 4060（显存 ≥8GB）",
    ram: "32GB",
    disk: "NVMe ≥50GB",
    can: { zh: "AI 作图 · 图片 / 视频换脸", en: "AI image gen · photo / video face swap" },
  },
  {
    tier: { zh: "标准版", en: "Standard" },
    gpu: "RTX 4070 Ti Super（显存 ≥16GB）",
    ram: "32GB",
    disk: "NVMe ≥100GB",
    can: { zh: "直播实时换脸 · 实时变声 · 直播推流", en: "Live face swap · voice changer · streaming" },
  },
  {
    tier: { zh: "专业版", en: "Pro" },
    gpu: "RTX 4090（显存 ≥24GB）",
    ram: "64GB",
    disk: "NVMe ≥200GB",
    can: { zh: "直播换脸 + 克隆音同传 · 多路会话", en: "Live swap + cloned-voice interpreting · multi-session" },
  },
  {
    tier: { zh: "旗舰版（私有部署）", en: "Flagship (private deployment)" },
    gpu: "RTX 4090 / 5090 × 2–4 台",
    ram: "每台 64GB · 万兆内网",
    disk: "NVMe ≥1TB",
    can: { zh: "私有化集群 · 多平台同播 · 分机热备", en: "Private cluster · multi-platform · hot standby" },
  },
];

/** 外设与配件推荐（按需选配；影响换脸贴合度与克隆音质的关键是光线与收音）。 */
export interface AccessoryRow {
  cat: { zh: string; en: string };
  entry: { zh: string; en: string };
  pro: { zh: string; en: string };
  note: { zh: string; en: string };
}

export const ACCESSORIES: AccessoryRow[] = [
  {
    cat: { zh: "摄像头", en: "Camera" },
    entry: { zh: "罗技 C920 / C922（1080p30）", en: "Logitech C920 / C922 (1080p30)" },
    pro: { zh: "罗技 Brio 4K · OBSBOT Tiny 2 云台", en: "Logitech Brio 4K · OBSBOT Tiny 2" },
    note: { zh: "实时换脸的输入源；光线比像素更重要，正面柔光显著提升贴合稳定", en: "Live-swap input; soft frontal light matters more than pixels" },
  },
  {
    cat: { zh: "麦克风", en: "Microphone" },
    entry: { zh: "Fifine K688 · 罗德 NT-USB Mini", en: "Fifine K688 · RØDE NT-USB Mini" },
    pro: { zh: "Shure MV7+ · SM7B + 声卡", en: "Shure MV7+ · SM7B + interface" },
    note: { zh: "克隆采样 30 秒即可；安静环境、离嘴 15–20cm、加防喷罩", en: "30s sample is enough; quiet room, 15–20cm, pop filter" },
  },
  {
    cat: { zh: "手机（中控 / 竖屏位）", en: "Phone (control / vertical)" },
    entry: { zh: "闲置安卓旗舰（骁龙 8 Gen2 级，小米 13 / 一加 11）", en: "Spare Android flagship (SD 8 Gen2 class)" },
    pro: { zh: "iPhone 15 Pro+ 或当年安卓旗舰", en: "iPhone 15 Pro+ or current flagship" },
    note: { zh: "直播中控监看、竖屏机位、Telegram 语音 / 视频演示终端", en: "Stream monitoring, vertical camera, Telegram voice/video endpoint" },
  },
  {
    cat: { zh: "采集卡 & 灯光", en: "Capture & lighting" },
    entry: { zh: "环形补光灯", en: "Ring light" },
    pro: { zh: "Elgato HD60 X + 平板灯 ×2（45° 打光）", en: "Elgato HD60 X + 2 panel lights (45°)" },
    note: { zh: "相机 / 手机 HDMI 进电脑走采集卡；均匀布光消除换脸边缘阴影", en: "HDMI-in via capture card; even lighting removes swap edge shadows" },
  },
  {
    cat: { zh: "绿幕 & 电源", en: "Green screen & power" },
    entry: { zh: "便携绿幕 / 纯色背景布", en: "Portable green screen" },
    pro: { zh: "UPS 不间断电源", en: "UPS backup power" },
    note: { zh: "背景替换边缘更干净；UPS 防直播中途断电掉线", en: "Cleaner background swap edges; UPS keeps streams alive" },
  },
];

/** 效果演示视频位：ready=true 的直接播放；false 显示「制作中」占位。
 *  真实引擎输出标 real=true（最有说服力）；AI 概念片入库时请在视频内加「概念演示」角标。
 *  文件放 public/videos/showcase/<key>.mp4（+ 可选 <key>-en.mp4），改 ready 后重新部署即可。 */
export interface ShowcaseVideo {
  key: string;
  ready: boolean;
  real?: boolean;
  src: string;
  srcEn?: string;
  poster?: string;
  posterEn?: string;
  title: { zh: string; en: string };
  desc: { zh: string; en: string };
}

export const SHOWCASE_VIDEOS: ShowcaseVideo[] = [
  // 2026-08-04 下架：数字人口播 / 直播换脸换声 / 视频换脸前后对比（ready=false，ShowcaseGrid 不渲染）
  {
    key: "avatar",
    ready: false,
    real: true,
    src: "/showcase/real/digital-human.mp4",
    srcEn: "/showcase/real/digital-human-en.mp4",
    poster: "/showcase/real/digital-human-poster.png",
    posterEn: "/showcase/real/digital-human-en-poster.jpg",
    title: { zh: "数字人口播", en: "Digital-human presenter" },
    desc: { zh: "一张照片 + 一段文案 → 口型精准的口播视频（真实引擎输出）", en: "One photo + a script → lip-accurate presenter video (real engine output)" },
  },
  {
    key: "voice",
    ready: true,
    real: true,
    src: "/videos/showcase/voice.mp4",
    srcEn: "/videos/showcase/voice-en.mp4",
    poster: "/videos/showcase/voice-poster.jpg",
    posterEn: "/videos/showcase/voice-en-poster.jpg",
    title: { zh: "声音克隆 · 情感 TTS", en: "Voice cloning · emotional TTS" },
    desc: { zh: "30 秒采样克隆音色，多语种带情感朗读", en: "Clone from a 30s sample, emotional multilingual speech" },
  },
  {
    key: "interp",
    ready: true,
    real: true,
    src: "/videos/showcase/interp.mp4",
    srcEn: "/videos/showcase/interp-en.mp4",
    poster: "/videos/showcase/interp-poster.jpg",
    posterEn: "/videos/showcase/interp-en-poster.jpg",
    title: { zh: "克隆音实时同传", en: "Real-time interpreting in your voice" },
    desc: { zh: "中文进、英文出——听到的仍是你自己的声音", en: "Mandarin in, English out — still your own voice" },
  },
  {
    key: "studio",
    ready: true,
    real: true,
    src: "/videos/showcase/studio.mp4",
    srcEn: "/videos/showcase/studio-en.mp4",
    poster: "/videos/showcase/studio-poster.jpg",
    posterEn: "/videos/showcase/studio-en-poster.jpg",
    title: { zh: "换发型 · 定妆 · 试衣", en: "Hair · makeup · try-on" },
    desc: { zh: "开播前预览妆造，一键切换整套形象", en: "Preview looks before going live, switch styles in one click" },
  },
];

// 私有授权 / 买断价目表已于 2026-08-04 定价改版移除：私有化部署与企业定制统一归
// 旗舰版 Flagship（咨询客服获取报价方案），不再挂牌固定年费 / 买断价，防双报价单打架。

/** 远程代部署：帮你选设备、装好客户端与环境、跑通第一个 demo（约 1 小时），当场采集指纹签发授权。 */
export const REMOTE_INSTALL = {
  price: 99,
  name: { zh: "远程代部署", en: "Remote install service" },
  desc: {
    zh: "含设备选型建议：远程装好客户端与环境并跑通第一个 demo，当场绑定机器指纹开通授权",
    en: "Includes hardware advice: we install everything remotely, run your first demo, and activate your license on the spot",
  },
};

export function tierPrice(t: Tier, period: Period): number {
  if (period === "monthly") return t.monthly;
  if (period === "quarterly") return t.quarterly ?? t.monthly * QUARTER_MONTHS;
  return t.annual ?? t.monthly * ANNUAL_MONTHS;
}
