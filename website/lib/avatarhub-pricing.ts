// AvatarHub 客户端（会员订阅 / 私有授权）定价单一真相 — /order 购买面板与
// /download 下载页共用。与引擎授权档（trial/standard/pro/enterprise）一一对应。
// 商业模式：我们不提供算力——设备用户自备（可代选配），我们协助部署；
// 引擎在用户本机运行，因此不按字符/张数/时长计费，档位差异只在能力、画质、并发与服务。
// 改价只改这里；lib/pricing.ts 是官网服务类 SKU（保持不动，两套并存）。

export type Period = "monthly" | "annual";

/** 年付 = 10 个月价（送 2 个月）；首年再 8 折。 */
export const ANNUAL_MONTHS = 10;
export const FIRST_YEAR_PROMO = 0.8;

/** TRC20 收款地址：在 .env.local 配 NEXT_PUBLIC_USDT_ADDR；未配置时页面引导找客服取地址（防呆）。 */
export const USDT_ADDR = process.env.NEXT_PUBLIC_USDT_ADDR || "";

export interface Tier {
  key: string;
  edition: "trial" | "standard" | "pro" | "enterprise";
  monthly: number; // 0 = 免费试用
  hot?: boolean;
  name: { zh: string; en: string };
  audience: { zh: string; en: string };
  feats: { zh: string[]; en: string[] };
}

export const TIERS: Tier[] = [
  {
    key: "trial",
    edition: "trial",
    monthly: 0,
    name: { zh: "体验版 Trial", en: "Trial" },
    audience: { zh: "14 天免费试用", en: "14-day free trial" },
    feats: {
      zh: ["核心功能全开放试用", "本机算力 · 用量不限", "720p · 合规水印", "1 路会话"],
      en: ["All core features to try", "Your hardware · unlimited usage", "720p · compliance watermark", "1 session"],
    },
  },
  {
    key: "starter",
    edition: "standard",
    monthly: 39,
    name: { zh: "入门版 Starter", en: "Starter" },
    audience: { zh: "个人 / 起步 · 单机", en: "Personal · single machine" },
    feats: {
      zh: ["声音克隆 + TTS 不限量", "图片 / 视频换脸不限量", "720p 高清 · 2 路会话", "社区支持"],
      en: ["Unlimited voice clone + TTS", "Unlimited photo/video face swap", "720p HD · 2 sessions", "Community support"],
    },
  },
  {
    key: "standard",
    edition: "standard",
    monthly: 99,
    name: { zh: "标准版 Standard", en: "Standard" },
    audience: { zh: "内容创作者 · 单机", en: "Creators · single machine" },
    feats: {
      zh: ["入门版全部能力", "1080p 超清档", "直播推流 · 虚拟摄像头", "克隆音同传 · 情感 TTS", "工单支持"],
      en: ["Everything in Starter", "1080p ultra preset", "Live streaming · virtual camera", "Cloned-voice interpreting · emotional TTS", "Ticket support"],
    },
  },
  {
    key: "pro",
    edition: "pro",
    monthly: 249,
    hot: true,
    name: { zh: "专业版 Pro", en: "Pro" },
    audience: { zh: "工作室 / MCN", en: "Studios / MCN" },
    feats: {
      zh: ["标准版全部能力", "去水印 · 双人换脸", "口播极致档 · 变声 API", "8 路并发会话", "云端 S2S 同传 · 优先支持"],
      en: ["Everything in Standard", "No watermark · duo swap", "Best lip-sync preset · VC API", "8 concurrent sessions", "Cloud S2S interpreting · priority support"],
    },
  },
  {
    key: "flagship",
    edition: "enterprise",
    monthly: 699,
    name: { zh: "旗舰版 Flagship", en: "Flagship" },
    audience: { zh: "企业 / 多机集群", en: "Enterprise / multi-node" },
    feats: {
      zh: ["Pro 全部能力", "分机拓扑 · 多平台多路直播", "站点授权 · 数据看板", "专属对接 · 远程调优", "每年 1 次免费代部署"],
      en: ["Everything in Pro", "Multi-node · multi-platform streaming", "Site license · dashboard", "Dedicated support · remote tuning", "1 free managed install / year"],
    },
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
    tier: { zh: "体验 / 入门版", en: "Trial / Starter" },
    gpu: "RTX 3060 12GB / 4060（显存 ≥8GB）",
    ram: "32GB",
    disk: "NVMe ≥50GB",
    can: { zh: "图片换脸 · 声音克隆 · TTS · 720p 实时换脸", en: "Photo swap · voice clone · TTS · 720p live swap" },
  },
  {
    tier: { zh: "标准版", en: "Standard" },
    gpu: "RTX 4070 Ti Super（显存 ≥16GB）",
    ram: "32GB",
    disk: "NVMe ≥100GB",
    can: { zh: "1080p 视频换脸 · 直播推流 · 克隆音同传", en: "1080p video swap · live streaming · interpreting" },
  },
  {
    tier: { zh: "专业版", en: "Pro" },
    gpu: "RTX 4090（显存 ≥24GB）",
    ram: "64GB",
    disk: "NVMe ≥200GB",
    can: { zh: "1080p 超清直播 · 双人换脸 · 口播极致 · 多路会话", en: "1080p ultra live · duo swap · best lip-sync · multi-session" },
  },
  {
    tier: { zh: "旗舰版（多机）", en: "Flagship (multi-node)" },
    gpu: "RTX 4090 / 5090 × 2–4 台",
    ram: "每台 64GB · 万兆内网",
    disk: "NVMe ≥1TB",
    can: { zh: "8 路并发 · 多平台同播 · 分机热备", en: "8 concurrent · multi-platform · hot standby" },
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
  {
    key: "avatar",
    ready: true,
    real: true,
    src: "/showcase/real/digital-human.mp4",
    srcEn: "/showcase/real/digital-human-en.mp4",
    poster: "/showcase/real/digital-human-poster.png",
    posterEn: "/showcase/real/digital-human-en-poster.jpg",
    title: { zh: "数字人口播", en: "Digital-human presenter" },
    desc: { zh: "一张照片 + 一段文案 → 口型精准的口播视频（真实引擎输出）", en: "One photo + a script → lip-accurate presenter video (real engine output)" },
  },
  {
    key: "live",
    ready: true,
    real: true,
    src: "/videos/showcase/live.mp4",
    srcEn: "/videos/showcase/live-en.mp4",
    poster: "/videos/showcase/live-poster.jpg",
    posterEn: "/videos/showcase/live-en-poster.jpg",
    title: { zh: "直播实时换脸换声", en: "Live face & voice swap" },
    desc: { zh: "摄像头输入 → 换脸 + 变声同步输出，低延迟直播可用", en: "Camera in → swapped face + voice out, stream-ready latency" },
  },
  {
    key: "faceswap",
    ready: true,
    real: true,
    src: "/videos/showcase/faceswap.mp4",
    srcEn: "/videos/showcase/faceswap-en.mp4",
    poster: "/videos/showcase/faceswap-poster.jpg",
    posterEn: "/videos/showcase/faceswap-en-poster.jpg",
    title: { zh: "视频换脸 · 前后对比", en: "Video face swap · before / after" },
    desc: { zh: "上传视频一键换脸，1080p 细节与光影保留", en: "One-click video swap, 1080p detail & lighting preserved" },
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

export interface LicenseRow {
  name: { zh: string; en: string };
  edition: string;
  yearly: { zh: string; en: string };
  buyout: { zh: string; en: string };
  desc: { zh: string; en: string };
}

export const LICENSES: LicenseRow[] = [
  {
    name: { zh: "标准授权 Standard", en: "Standard license" },
    edition: "standard",
    yearly: { zh: "1,280 / 年", en: "1,280 / yr" },
    buyout: { zh: "2,980 买断", en: "2,980 lifetime" },
    desc: { zh: "单机 · HD · 2 并发 · 超清档", en: "Single machine · HD · 2 concurrent" },
  },
  {
    name: { zh: "专业授权 Pro", en: "Pro license" },
    edition: "pro",
    yearly: { zh: "2,980 / 年", en: "2,980 / yr" },
    buyout: { zh: "6,980 买断", en: "6,980 lifetime" },
    desc: { zh: "去水印 · 多副本 · 8 并发 · 口播极致", en: "No watermark · multi-replica · 8 concurrent" },
  },
  {
    name: { zh: "企业授权 Enterprise", en: "Enterprise license" },
    edition: "enterprise",
    yearly: { zh: "12,800 起 / 年", en: "from 12,800 / yr" },
    buyout: { zh: "按规模报价", en: "quoted by scale" },
    desc: { zh: "分机集群 · 站点授权 · S2S", en: "Cluster · site license · S2S" },
  },
  {
    name: { zh: "数字人形象买断", en: "Digital-human avatar buyout" },
    edition: "—",
    yearly: { zh: "—", en: "—" },
    buyout: { zh: "998 / 个", en: "998 each" },
    desc: { zh: "永久角色形象 · 克隆声绑定", en: "Permanent avatar · bound cloned voice" },
  },
  {
    name: { zh: "私有部署实施服务", en: "Private deployment service" },
    edition: "—",
    yearly: { zh: "—", en: "—" },
    buyout: { zh: "1,280 起", en: "from 1,280" },
    desc: { zh: "远程选型 + 部署 + 调试 + 培训", en: "Remote setup + tuning + training" },
  },
];

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
  return period === "monthly" ? t.monthly : t.monthly * ANNUAL_MONTHS;
}
