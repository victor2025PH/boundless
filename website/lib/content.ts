// 幻境 STUDIO 五档报价单一真相在 lib/avatarhub-pricing.ts::TIERS（2026-08-04 单源化）：
// 本文件所有幻境档位行 / 起价数字一律派生，勿再手写（改价只改 TIERS 一处）。
import { studioTierRows, studioTier, tierPriceLabel, STUDIO_PAID_FROM } from "./avatarhub-pricing";

export type Lang = "zh" | "en";

export interface PricingRow {
  plan: string;
  price: string;
  detail: string;
  /** 自助 SKU 的 /order 深链 plan 键（lib/order-lines.ts 的 tier key）；缺省 = 走客服。 */
  order?: string;
}

export interface Solution {
  id: string;
  tag: string;
  title: string;
  desc: string;
  features: string[];
  pricing: PricingRow[];
  highlight?: boolean;
}

export interface Plan {
  name: string;
  priceMonthly: string;
  priceYearly: string;
  desc: string;
  features: string[];
  highlight?: boolean;
  /** 自助下单深链键（/order?plan=<key>）；缺省 = CTA 回落客服。 */
  plan?: string;
}

export interface Dict {
  nav: {
    solutions: string;
    translate: string;
    demo: string;
    autochat: string;
    cases: string;
    engage: string;
    pricing: string;
    about: string;
    contact: string;
    cta: string;
  };
  hero: {
    badge: string;
    title: string;
    titleAccent: string;
    titleLines: string[];
    rotating: string[];
    subtitle: string;
    trustline: string;
    ctaPrimary: string;
    ctaSecondary: string;
    stats: { value: string; label: string }[];
  };
  trust: {
    platformsLabel: string;
    /** 第一层「已深度对接」：name=brandIcons 字形键；label=展示名覆写（缺省用 name）；note=能力一句话 */
    platformsLive: { name: string; label?: string; note: string }[];
    platformsComingLabel: string;
    /** 第二层「陆续接入」：灰阶 + 角标，仅表达路线图（口径见 docs/claims.md 平台墙分层行） */
    platformsComing: string[];
    statsTitle: string;
    statsSubtitle: string;
    /** 前 4 项渲染为主数字大卡，其余为次级紧凑卡；sub=给非技术读者的一句人话 */
    stats: { value: string; suffix: string; label: string; sub?: string }[];
    testimonialsTitle: string;
    testimonials: { quote: string; name: string; role: string }[];
    disclaimer: string;
  };
  plans: {
    title: string;
    subtitle: string;
    monthly: string;
    yearly: string;
    save: string;
    popular: string;
    perMonth: string;
    cta: string;
    items: Plan[];
  };
  orderSteps: {
    title: string;
    subtitle: string;
    steps: { title: string; desc: string }[];
  };
  faq: {
    title: string;
    subtitle: string;
    items: { q: string; a: string }[];
  };
  realtime: {
    badge: string;
    title: string;
    subtitle: string;
    videoNote: string;
    features: { icon: string; title: string; desc: string }[];
    stepsTitle: string;
    steps: { title: string; desc: string }[];
    hardwareTitle: string;
    hardwareNote: string;
    hardware: { tier: string; gpu: string; use: string }[];
    plansTitle: string;
    plansNote: string;
    plans: {
      name: string;
      tag?: string;
      price: string;
      unit: string;
      specs: string[];
      cta: string;
      highlight?: boolean;
    }[];
    extrasTitle: string;
    extras: string[];
    availability: string;
    capacityNote: string;
    cta: string;
  };
  faceswap: {
    badge: string;
    title: string;
    subtitle: string;
    tabCustom: string;
    tabTemplate: string;
    uploadFace: string;
    uploadFaceHint: string;
    uploadTarget: string;
    uploadTargetHint: string;
    pickTemplate: string;
    templates: { id: string; name: string; file: string }[];
    consent: string;
    button: string;
    processing: string;
    resultTitle: string;
    download: string;
    again: string;
    privacy: string;
    errConfig: string;
    errNoConsent: string;
    errNoImage: string;
    errSize: string;
    errGeneric: string;
  };
  solutionsSection: {
    title: string;
    subtitle: string;
  };
  solutions: Solution[];
  /** 首页「私有定制」报价大表已于 2026-08-04 下线（Pricing.tsx 已删）；
   *  仅剩 note 一个字段——Telegram 小程序 /app 价格页的挂牌说明还在消费。 */
  pricingSection: {
    note: string;
  };
  about: {
    title: string;
    subtitle: string;
    points: { title: string; desc: string }[];
  };
  community: {
    badge: string;
    title: string;
    subtitle: string;
    perks: string[];
    cta: string;
    groupCta: string;
  };
  gate: {
    badge: string;
    title: string;
    subtitle: string;
    joinChannel: string;
    joinGroup: string;
    joinedChannel: string;
    joinedGroup: string;
    verify: string;
    checking: string;
    webNote: string;
    unlockedTitle: string;
    unlockedDesc: string;
    codeLabel: string;
    code: string;
    cta: string;
    notYet: string;
  };
  swap: {
    before: string;
    after: string;
    dragHint: string;
    liveTag: string;
    hudEngine: string;
    hudFps: string;
    hudLatency: string;
    callStatus: string;
    you: string;
    theySee: string;
    faceVoice: string;
    voiceCloning: string;
  };
  showcaseSection: {
    title: string;
    subtitle: string;
  };
  chatDemo: {
    badge: string;
    title: string;
    desc: string;
    features: string[];
    translatedTag: string;
    replyName: string;
    typing: string;
    messages: { name: string; flag: string; text: string; translated: string }[];
    reply: { text: string; translated: string };
  };
  voiceDemo: {
    badge: string;
    title: string;
    desc: string;
    features: string[];
    original: string;
    cloned: string;
    langsLabel: string;
    langs: string[];
  };
  deployDemo: {
    badge: string;
    title: string;
    desc: string;
    features: string[];
    cloudLabel: string;
    localLabel: string;
    rows: { label: string; cloud: boolean; local: boolean }[];
  };
  digitalDemo: {
    badge: string;
    title: string;
    desc: string;
    features: string[];
    tags: string[];
  };
  compare: {
    badge: string;
    title: string;
    subtitle: string;
    cols: string[];
    rows: { label: string; us: string; them: string; manual: string }[];
  };
  autochat: {
    badge: string;
    title: string;
    subtitle: string;
    features: { icon: string; title: string; desc: string }[];
    compareTitle: string;
    compareNote: string;
    badLabel: string;
    goodLabel: string;
    compare: { src: string; bad: string; good: string }[];
    scenariosLabel: string;
    scenarios: string[];
    cta: string;
    demo: {
      inbox: string;
      personaName: string;
      translatedTag: string;
      autoTag: string;
      voiceTag: string;
      typing: string;
      incoming: { name: string; flag: string; text: string; translated: string };
      reply: { text: string; translated: string };
      voiceLen: string;
    };
  };
  engage: {
    badge: string;
    title: string;
    subtitle: string;
    selectorTitle: string;
    youLabel: string;
    weLabel: string;
    selector: { id: string; label: string }[];
    models: {
      id: string;
      badge: string;
      name: string;
      tagline: string;
      you: string;
      we: string;
      price: string;
      priceNote: string;
      points: string[];
      cta: string;
      highlight?: boolean;
    }[];
    serviceTiersLabel: string;
    extrasLabel: string;
    invest: {
      roiTitle: string;
      roiRows: { label: string; value: string }[];
      roiNote: string;
      flowTitle: string;
      flow: string[];
      compliance: string;
    };
    matrixTitle: string;
    matrixCols: string[];
    matrix: { label: string; a: string; b: string; c: string }[];
  };
  roi: {
    badge: string;
    title: string;
    subtitle: string;
    inputs: { agents: string; salary: string; leads: string; aov: string; conv: string };
    units: { agents: string; salary: string; leads: string; aov: string; conv: string };
    resultSaveLabel: string;
    resultRevenueLabel: string;
    resultNetLabel: string;
    resultRoiLabel: string;
    resultYearLabel: string;
    planLabel: string;
    perMonth: string;
    assumptionsTitle: string;
    assumptions: string[];
    disclaimer: string;
    cta: string;
  };
  cases: {
    badge: string;
    title: string;
    subtitle: string;
    items: { scene: string; metric: string; metricLabel: string; quote: string; name: string; role: string; img: string }[];
    galleryTitle: string;
    gallerySubtitle: string;
    translatedTag: string;
    replyTag: string;
    gallery: { lang: string; flag: string; incoming: string; translated: string; reply: string }[];
    disclaimer: string;
  };
  lead: {
    title: string;
    subtitle: string;
    name: string;
    contact: string;
    interest: string;
    message: string;
    namePh: string;
    contactPh: string;
    messagePh: string;
    interests: string[];
    submit: string;
    submitting: string;
    successTitle: string;
    successDesc: string;
    error: string;
    contactInvalid: string;
    privacy: string;
  };
  contact: {
    title: string;
    subtitle: string;
    telegram: string;
    telegramHandle: string;
    scanHint: string;
    usdt: string;
    usdtNote: string;
    networks: string;
    responseTime: string;
    compliance: string;
    complianceNote: string;
    cta: string;
  };
  footer: {
    rights: string;
    disclaimerTitle: string;
    disclaimer: string;
    links: string[];
  };
}

const zh: Dict = {
  nav: {
    solutions: "业务能力",
    translate: "实时翻译",
    demo: "实时换脸",
    autochat: "AI 成交聊天",
    cases: "案例",
    engage: "合作方式",
    pricing: "价格",
    about: "关于我们",
    contact: "联系下单",
    cta: "立即咨询",
  },
  hero: {
    badge: "无界科技 · 跨境沟通 AI · 统一收件箱 · 私有部署",
    title: "跟全球客户聊天",
    titleAccent: "像跟老乡聊天一样自然",
    titleLines: ["跟全球客户聊天", "像跟老乡聊天", "一样自然"],
    rotating: ["30+ 语种拟人互译秒回", "AI 用你的人设 7×24 跟单", "多平台消息一个收件箱", "关键时刻一键人工接管", "私有部署 · 数据不出网"],
    subtitle:
      "把 WhatsApp / Telegram / LINE / Messenger 全部接进一个收件箱：客户说什么语言都行，AI 实时拟人互译、按你的人设自动答疑、跟单、促成交，重要节点你随时一键接管。全程支持私有部署，数据不出你的服务器。",
    trustline: "7×24 看门狗自愈生产运行 · 1100+ 自动化测试护航 · 数据不出网",
    ctaPrimary: "免费咨询 · 拿成交方案",
    ctaSecondary: "查看套餐与价格",
    stats: [
      { value: "24H", label: "看门狗自愈全天候运行" },
      { value: "30+", label: "语种拟人互译" },
      { value: "5", label: "大平台统一接入" },
      { value: "1100+", label: "自动化回归测试" },
    ],
  },
  solutionsSection: {
    title: "三大产品系 · 一个无界底座",
    subtitle: "智连获客成交（智聊·智拓）、通达同声传译（通传）、幻境数字分身（幻声·幻影）——三大产品系共享无界底座，私有部署、数据不出网，按需单独选用或组合。",
  },
  solutions: [
    {
      id: "chatx",
      tag: "智聊 ChatX",
      title: "AI 成交聊天 · 统一收件箱",
      desc: "多平台消息聚合进一个工作台：AI 按你的人设自动答疑、跟单、促成交，拟人翻译内建，重要节点一键人工接管；支持私有部署。",
      features: ["多平台统一收件箱", "AI 人设自动跟单", "内建拟人翻译", "一键人工接管"],
      highlight: true,
      pricing: [
        { plan: "入门", price: "58 / 月", detail: "3 个聊天账号 · 1 个平台", order: "autochat-entry" },
        { plan: "团队", price: "198 / 月", detail: "10 账号 · 全平台 · AI 自动成交", order: "autochat-team" },
        { plan: "旗舰", price: "598 / 月", detail: "50 账号 · 人工接管 · 数据看板", order: "autochat-flagship" },
      ],
    },
    {
      id: "reach",
      tag: "智拓 ReachX",
      title: "真机获客 · 智拓 ReachX（邀请制）",
      desc: "真机集群获客方案：多号管理、群成员提取、打招呼引流。涉及平台合规边界，按团队场景评估后私有交付，不做公开自助开通。",
      features: ["真机多号管理", "群成员批量提取", "私有化交付", "邀请制评估"],
      pricing: [
        { plan: "私有化部署", price: "按规模报价", detail: "真机获客集群，按设备数与规模定制交付" },
      ],
    },
    {
      id: "voice",
      tag: "幻声 VoiceX",
      title: "声音克隆 VoiceClone",
      desc: "几十秒样本零样本克隆任意音色，三引擎自动择优（Fish 实时 / Qwen3 首包 ≈97ms 十语种 / VoxCPM 48kHz 可商用），多语种 TTS + 实时变声 + 情感可调。",
      features: ["秒级零样本克隆", "三引擎自动择优", "10+ 语种合成", "实时变声 · 情感可调"],
      // 2026-08-04：声音能力纳入幻境 STUDIO 五档会员（档位行由 TIERS 派生，改价改 avatarhub-pricing.ts）。
      pricing: studioTierRows("zh"),
    },
    {
      // 2026-08-04 通译并入智聊：本卡从独立产品「通译 LingoX」改为智聊的翻译专项套餐卡
      //（id/SKU/深链不变，授权层 lingox-* 照旧履约；主推位 highlight 移交智聊主卡）。
      id: "translate",
      tag: "智聊 ChatX · 翻译",
      title: "跨境聊天翻译 · 翻译专项套餐",
      desc: "多平台文字 + 语音双向实时翻译，术语表锁定专有名词、翻译记忆省成本，统一收件箱沉淀客户资产——让不会外语的团队在 WhatsApp / Telegram / LINE 上即时跟全球客户对话。翻译能力已内置在智聊 ChatX 客户端，按坐席 + 字符额度授权，下载智聊即可使用。",
      features: ["多平台双向翻译", "术语锁定 · 翻译记忆", "统一收件箱 · 客户资产", "多模态（图片/语音）翻译"],
      // 定价与 lib/pricing.ts::translateOffers 同步（USD，2026-07-18 定价决议：竞品×2）；改价两处一起改。
      pricing: [
        { plan: "字符包", price: "59", detail: "一次性 · 150 万字符 + 术语库 + 翻译记忆", order: "translate-charpack" },
        { plan: "团队", price: "99 / 月", detail: "300 万字符/月 + 多坐席收件箱 + 客户 journey + 漏斗计数", order: "translate-team" },
        { plan: "专业", price: "198 / 月", detail: "不限字符 + 多模态翻译 + 置信度/引擎健康", order: "translate-pro" },
      ],
    },
    {
      id: "interpret",
      tag: "通传 VoxX",
      title: "克隆音实时同传 + 双语字幕",
      desc: "用你自己的克隆声做双向语音同传，术语表锁定专有名词、支持抢话打断；OBS 拖一个链接直播间即出实时双语字幕，散场一键导 SRT。会议、直播、连麦全覆盖。目前以内测方式交付：预约演示，按场景配置。",
      features: ["克隆音双向同传", "术语锁定 · 抢话打断", "OBS 双语字幕", "一键通话套餐"],
      pricing: [
        { plan: "内测预约", price: "按需报价", detail: "会议 / 直播场景评估后交付" },
        { plan: "私有化部署", price: "按需报价", detail: "买断 + 年度维护" },
      ],
    },
    {
      id: "private-ai",
      tag: "无界底座",
      title: "自主可控 AI · 私有化部署",
      desc: "把大模型部署到你自己的服务器：数据不出本地、不依赖公有云，输出风格与行业知识库按业务自由微调，完全自主可控。",
      features: ["私有部署不出网", "数据主权自主可控", "行业知识库微调", "输出风格自定义"],
      pricing: [
        { plan: "云端 API", price: "≈1.2x token", detail: "充值制，私有中转不留存" },
        { plan: "单机私有部署", price: "1600 起", detail: "一次性，含部署调试" },
        { plan: "企业私有集群", price: "6000 起", detail: "按规模报价" },
        { plan: "模型微调 / 定制", price: "1000 起", detail: "按任务" },
      ],
    },
    {
      id: "digital-human",
      tag: "幻影 LiveX",
      title: "高清活体数字人 / 虚拟主播",
      desc: "克隆形象 + 克隆声 + 口型同步的活体分身，会眨眼、会摆头、有微表情——不是死图对口型。一路直推 WebRTC / OBS，旗舰算力档 25fps 高清、亚秒级首帧（以部署环境为准）。",
      features: ["活体形象 + 克隆声", "口型同步 · 会眨眼摆头", "WebRTC / OBS 直推", "虚拟背景 · 口播成片"],
      // 2026-08-04：数字人能力纳入幻境 STUDIO 五档（档位行由 TIERS 派生）；旧「198 起 / 形象买断 798」下线防双报价。
      pricing: studioTierRows("zh"),
    },
    {
      id: "video-dubbing",
      tag: "幻影 LiveX",
      title: "AI 视频翻译配音",
      desc: "上传视频自动翻译、克隆原声配音、对口型，出海短视频刚需。能力随幻境 STUDIO 会员开通；企业级批量矩阵请咨询旗舰版私有部署。",
      features: ["自动字幕翻译", "原声克隆配音", "口型对齐", "批量处理"],
      pricing: [
        { plan: "专业版起", price: tierPriceLabel(studioTier("pro"), "zh"), detail: "含直播换脸 · 同传 · 见幻境 STUDIO ", order: "pro" },
        { plan: "旗舰版", price: tierPriceLabel(studioTier("flagship"), "zh"), detail: "私有部署 · 短视频矩阵定制", order: "flagship" },
      ],
    },
  ],
  pricingSection: {
    note: `幻境 STUDIO ：免费换脸+水印 / 入门 ${studioTier("starter").monthly} / 标准 ${studioTier("standard").monthly} / 专业 ${studioTier("pro").monthly}（月付；另有季付·年付挂牌）/ 旗舰咨询报价。智聊（成交 + 翻译）套餐见其产品线；超出清单的需求按场景定制。`,
  },
  trust: {
    platformsLabel: "深度对接的沟通平台",
    platformsLive: [
      { name: "Telegram", note: "协议级接入 · 消息 / 语音 / 媒体全能力" },
      { name: "WhatsApp", note: "双向收发 · 语音 · 媒体" },
      { name: "LINE", note: "双向收发 · 媒体 · 好友欢迎" },
      { name: "Messenger", note: "网页 + 移动 App 双链路" },
      { name: "Web", label: "网页客服", note: "官网 / 独立站即嵌即用" },
      { name: "Facebook", note: "真机获客 · 好友 / 群触达" },
    ],
    platformsComingLabel: "更多平台 · 陆续接入",
    platformsComing: ["Instagram", "TikTok", "X", "Discord", "WeChat", "Zalo", "Viber", "KakaoTalk", "Signal"],
    statsTitle: "用工程事实说话",
    statsSubtitle: "每个数字都有仓内实测记录与运行台账背书——可举证、可复现，拒绝形容词式吹牛。",
    stats: [
      { value: "1100", suffix: "+", label: "自动化回归测试（双引擎实测计数）", sub: "每一次改动都要先过这张回归网" },
      { value: "50", suffix: "/50", label: "多语种回译评测全数通过（2026-08 周批）", sub: "回译 + 语义双轨，每周六自动重测" },
      { value: "22", suffix: "/22", label: "断云演习本地模型全量接管", sub: "云端断链客户零感知，恢复自动闭合" },
      { value: "24", suffix: "/7", label: "生产运行 · 看门狗 5 分钟自愈巡检", sub: "凌晨三点挂了也会自己爬起来" },
      { value: "30", suffix: "+", label: "语种拟人互译（模型口径）", sub: "俚语与语气像本地人" },
      { value: "5", suffix: "", label: "大平台接入 · 统一收件箱", sub: "全平台消息一个工作台接住" },
      { value: "0.939", suffix: "", label: "回译语义均分（满分 1.0）", sub: "机器评审口径，周批持续追踪" },
      { value: "20", suffix: "+", label: "专项质量评测门禁", sub: "危机安全 / 人设一致性 / 图文一致性…" },
    ],
    testimonialsTitle: "为什么可信",
    testimonials: [
      {
        quote: "智聊 7×24 生产运行：看门狗每 5 分钟巡检、异常自动拉起，重启带冷却闸门与维护预告，前台坐席无感。",
        name: "生产部署",
        role: "来源：deploy/instances 运行记录",
      },
      {
        quote: "断云真流量演习：切断云端大模型后，22/22 条请求由本地模型接管出真话，客户端零感知，恢复后自动闭合。",
        name: "容灾演习",
        role: "来源：2026-07 断云演习记录",
      },
      {
        quote: "翻译走回译 + 语义双轨评测：宽语料 50/50 通过、语义均分 0.939；弱语对按周批数据自动切换更强引擎。",
        name: "翻译评测",
        role: "来源：translation_eval 周批（2026-08）",
      },
    ],
    disclaimer: "以上为内部工程实测与部署记录口径（2026-08），非对具体商业效果的承诺。",
  },
  plans: {
    title: "AI 成交聊天 · 套餐",
    subtitle: "聚合 + AI 拟人翻译 + AI 自动成交 + 人设语音，按账号规模选档，年付更划算。",
    monthly: "月付",
    yearly: "年付",
    save: "省 15%",
    popular: "最受欢迎",
    perMonth: "/ 月",
    cta: "选择此套餐",
    items: [
      {
        name: "入门",
        priceMonthly: "58",
        priceYearly: "50",
        desc: "小团队 / 个人起步",
        features: ["3 个聊天账号", "AI 拟人翻译", "1 个平台", "基础声音克隆体验"],
        plan: "autochat-entry",
      },
      {
        name: "团队",
        priceMonthly: "198",
        priceYearly: "168",
        desc: "成长型团队首选",
        features: ["10 个聊天账号", "全平台聚合", "AI 自动成交回复", "人设语音消息", "优先客服"],
        highlight: true,
        plan: "autochat-team",
      },
      {
        name: "旗舰",
        priceMonthly: "598",
        priceYearly: "508",
        desc: "规模化 / 企业级",
        features: ["50 个聊天账号", "AI 自动成交 + 人设语音", "人工接管 + 知识库", "数据看板", "可选私有化部署"],
        plan: "autochat-flagship",
      },
    ],
  },
  orderSteps: {
    title: "三步即可开通",
    subtitle: "标准套餐全程自助：在线下单、付款到账、自动开通；定制方案随时找客服。",
    steps: [
      { title: "选择套餐", desc: "在下单页选择套餐与周期；企业定制或拿不准的，加 Telegram 客服帮你选。" },
      { title: "在线下单付款", desc: "支持 USDT / 银行卡；订单页实时显示到账进度，链接可收藏随时回查。" },
      { title: "到账自动开通", desc: "付款确认后系统自动签发授权码 / 凭证，订单页直接复制，即刻激活使用。" },
    ],
  },
  faq: {
    title: "常见问题",
    subtitle: "还有疑问？直接联系 Telegram 客服。",
    items: [
      { q: "支持哪些付款方式？", a: "标准套餐可在官网下单页自助购买，支持 USDT（TRC20）等结算，到账后自动开通；大额或企业定制可通过官方 Telegram 客服商定其它方式。" },
      { q: "支持私有化部署吗？", a: "支持。聊天聚合与自主可控 AI 均可部署到你的服务器，数据本地化、无云端上报。" },
      { q: "你们的 AI 翻译和谷歌翻译有什么不同？", a: "我们用 AI 翻译 + 对话技术，输出地道口语、地方俚语与文化语气，对方看不出你是外国人；不同于市面软件直接套谷歌等 API 的生硬直译。" },
      { q: "AI 能自动跟客户成交吗？人工能接管吗？", a: "可以。AI 以你的人设 7×24 自动接洽、答疑、跟进、促单转化，遇到关键节点人工可随时一键接管。" },
      { q: "声音克隆需要什么素材？", a: "仅需几十秒清晰人声样本即可零样本克隆；请确保你拥有该声音的授权。" },
      { q: "私有大模型和公有云 API 有什么区别？", a: "私有部署的大模型数据完全留在本地、不依赖公有云内容策略，可按你的业务自由微调知识库与输出风格，无云端上报，自主可控。" },
      { q: "可以按量付费吗？", a: "可以。多数业务同时提供订阅与按量加购，用多少付多少，灵活组合。" },
      { q: "如何确认我在和官方沟通、收款地址无误？", a: "官网只在订单页实时展示收款地址；客服只使用官网页面上列出的官方 Telegram 账号。任何『主动私聊你的客服』或第三方转发的地址，请一律回到订单页核对后再操作。" },
    ],
  },
  realtime: {
    badge: "旗舰服务 · 技术落地",
    title: "实时换脸 + 换声 · 私有化部署服务",
    subtitle: "我们把直播 / 视频通话级的实时换脸 + 声音克隆，部署到你自己的设备上，并按你的场景深度定制——硬件你自备，我们负责选型建议、部署落地、调试培训与长期支持。数据全程私有、不出网，适用于任何场景。",
    videoNote: "实时换脸演示 · 视频即将上线",
    features: [
      { icon: "cpu", title: "私有可控 · 数据不出机房", desc: "部署在你自己的设备，数据不出本地、不上传公网；产出物默认带 C2PA 可验真水印。" },
      { icon: "zap", title: "脸区原生 · 高清不糊", desc: "脸部走原生高分辨率通道，实测清晰度 4.5× 提升；720p 高清脸默认、1080p 超清档随选。" },
      { icon: "monitor", title: "虚拟背景 · 双人同框", desc: "直播实时换背景 / 绿幕不占显卡；双人各换各脸，负载吃紧自动降档不卡顿。" },
      { icon: "shield", title: "长期支持 · 开播体检", desc: "开播前 3 秒设备体检红灯先修再播；交付文档 + 培训 + 运维升级与远程协助。" },
    ],
    stepsTitle: "服务流程",
    steps: [
      { title: "需求沟通 & 选型", desc: "确认你的场景，给出硬件配置清单，你照单自行采购。" },
      { title: "远程部署", desc: "在你的设备上部署换脸 / 换声 / 数字人 / 私有大模型。" },
      { title: "场景定制调试", desc: "按你的平台与玩法调到最佳效果，并远程培训上手。" },
      { title: "验收 & 持续支持", desc: "交付文档与运维，提供后续升级和技术支持。" },
    ],
    hardwareTitle: "推荐硬件配置（你自购）",
    hardwareNote: "硬件由你采购、完全归你所有；我们只提供配置建议与部署服务，不转售算力。运行时 /api/hardware/guide 会按你的显卡给出每功能推荐档位。",
    hardware: [
      { tier: "入门 · 换脸/出片", gpu: "RTX 5070 Ti / 4080 16G", use: "1080p 实时换脸直播 · 图片/视频批量成片" },
      { tier: "专业 · 换脸+数字人", gpu: "RTX 4090 24G / 5080", use: "1080p 超清换脸 · 高清数字人 · 多场景并行" },
      { tier: "旗舰 · 全能工作站", gpu: "RTX 5090 32G（可双卡）", use: "25fps 高清活体数字人 + 克隆音 + 同传同驻 · 多路直播" },
    ],
    plansTitle: "服务套餐与报价",
    // 2026-08-04 定价改版：私有部署定制统一归幻境 STUDIO 旗舰版口径——不再挂一次性固定价，
    // 全部咨询客服获取报价方案；自助购买走 /order 的幻境 STUDIO 会员套餐（免费换脸起步，39 USD/月起）。
    plansNote: `私有部署定制 · 咨询客服获取报价方案；标准能力可在下单页自助购买幻境 STUDIO 会员（免费换脸起步，${STUDIO_PAID_FROM} USD/月起）`,
    availability: "本周可接 3 个部署排期 · 预约制（先约先得）",
    plans: [
      {
        name: "基础部署",
        tag: "单能力",
        price: "咨询报价",
        unit: "按需定制 · 含部署调试",
        specs: ["实时换脸 或 换声 任选其一", "远程部署 + 基础调试", "上手培训", "7 天技术支持"],
        cta: "Telegram 咨询",
      },
      {
        name: "创作者全能",
        tag: "推荐",
        price: "咨询报价",
        unit: "按需定制 · 含部署调试",
        specs: ["实时换脸 + 换声 + 数字人", "多场景深度调试", "上手培训 + 文档", "30 天技术支持"],
        cta: "Telegram 咨询",
        highlight: true,
      },
      {
        name: "全家桶",
        tag: "全能力",
        price: "咨询报价",
        unit: "按需定制 · 含部署调试",
        specs: ["换脸 + 换声 + 数字人", "自主可控私有大模型", "全场景定制调试", "30 天支持 + 1 月运维"],
        cta: "Telegram 咨询",
      },
    ],
    extrasTitle: "更多服务",
    extras: [
      "场景深度定制开发 · 咨询报价",
      "上门 / 驻场部署 · 咨询报价（含差旅）",
      "运维订阅 · 198 / 月 或 1998 / 年",
      "按次远程协助 · 160 / 小时",
    ],
    capacityNote: "硬件归你所有、数据私有不出网；我们提供从选型到部署、定制与运维的全流程技术服务，适用于任何场景。",
    cta: "Telegram 咨询定制",
  },
  faceswap: {
    badge: "免费体验 · 图片版",
    title: "图片换脸 · 免费体验",
    subtitle: "想先感受效果？上传照片，秒变职业照、超级英雄、宇航员。（实时换脸请看上方旗舰服务）",
    tabCustom: "自定义目标",
    tabTemplate: "模板换脸",
    uploadFace: "你的照片",
    uploadFaceHint: "正脸清晰、光线充足效果最佳",
    uploadTarget: "目标图片",
    uploadTargetHint: "你的脸会被换到这张图里",
    pickTemplate: "选择一个模板",
    templates: [],
    consent: "我确认对上传的照片拥有合法肖像授权，且不用于任何违法用途。",
    button: "开始换脸",
    processing: "AI 换脸中…通常需 30~60 秒，请稍候",
    resultTitle: "换脸结果",
    download: "下载图片",
    again: "再换一张",
    privacy: "图片仅用于本次换脸处理，不长期存储。",
    errConfig: "换脸服务正在配置中，敬请期待。",
    errNoConsent: "请先勾选肖像授权确认。",
    errNoImage: "请先上传照片。",
    errSize: "图片过大，请上传小于 8MB 的图片。",
    errGeneric: "换脸失败，请换一张更清晰的正脸照片重试。",
  },
  about: {
    title: "为什么选择无界科技",
    subtitle: "形象看得见、对话听得懂——技术自研、出海友好、私有可控。",
    points: [
      { title: "技术自研", desc: "声音 / 形象 / 对话 / 同传四大引擎自研可控，非简单套壳，实测指标可当场复现。" },
      { title: "私有可控", desc: "本地全栈部署，数据不出机房；产出物默认带 C2PA 可验真水印，政企合规首选。" },
      { title: "出海友好", desc: "多种结算方式、跨境无障碍，跨境团队即买即用。" },
      { title: "快速交付", desc: "标准业务即开即用，定制需求专属对接，交付含培训与运维。" },
    ],
  },
  community: {
    badge: "官方社群",
    title: "关注频道 · 加入交流群",
    subtitle: "频道第一时间发真实案例、新功能与限时折扣；进群和同行交流、领 AI 自动成交试用与专属优惠。",
    perks: ["真实案例与成果展示", "新功能 + 限时折扣", "进群领试用 · 同行交流"],
    cta: "关注频道",
    groupCta: "加入交流群",
  },
  gate: {
    badge: "专属解锁",
    title: "关注频道 + 进群，解锁专属优惠",
    subtitle: "关注官方频道并加入交流群，即可解锁专属折扣码与免费试用名额。",
    joinChannel: "① 关注频道",
    joinGroup: "② 加入交流群",
    joinedChannel: "✅ 已关注频道",
    joinedGroup: "✅ 已加入群",
    verify: "我已完成，校验解锁",
    checking: "校验中…",
    webNote: "在 Telegram 内打开本页（小程序）即可自动校验并解锁。",
    unlockedTitle: "🎉 已解锁专属权益",
    unlockedDesc: "凭专属折扣码联系客服，享报价优惠并锁定免费试用名额。",
    codeLabel: "你的专属折扣码",
    code: "HL-VIP",
    cta: "锁定试用名额 · 联系客服",
    notYet: "尚未检测到关注/进群，请先完成上面两步再校验。",
  },
  swap: {
    before: "原始",
    after: "换脸后",
    dragHint: "拖动滑块查看换脸前后",
    liveTag: "实时换脸中",
    hudEngine: "FACE SWAP ENGINE",
    hudFps: "HD 25 FPS",
    hudLatency: "首帧 0.9s",
    callStatus: "通话中",
    you: "你 · 真实",
    theySee: "对方看到",
    faceVoice: "换脸 + 换声",
    voiceCloning: "声音克隆中",
  },
  showcaseSection: {
    title: "看得见的能力",
    subtitle: "不只是说说——每项核心能力都给你一个可交互的真实演示。",
  },
  chatDemo: {
    badge: "对话 · 实时翻译",
    title: "聊天聚合 + 实时翻译",
    desc: "TG / LINE / WhatsApp / Messenger 多号聚合到一个收件箱，收发即时双向翻译，AI 自动回复、人工随时接管。",
    features: ["多平台多号聚合", "双向实时翻译", "AI 自动回复", "人工接管 + 知识库"],
    translatedTag: "已翻译",
    replyName: "AI 助手",
    typing: "对方正在输入…",
    messages: [
      { name: "Carlos", flag: "🇪🇸", text: "¿Hacen envíos a México?", translated: "你们发货到墨西哥吗？" },
      { name: "あやか", flag: "🇯🇵", text: "在庫はまだありますか？", translated: "还有库存吗？" },
    ],
    reply: { text: "Yes! We ship worldwide, 3–7 days.", translated: "可以！全球发货，3–7 天送达。" },
  },
  voiceDemo: {
    badge: "声音 · 三引擎克隆",
    title: "声音克隆 VoiceClone",
    desc: "几十秒清晰样本即可零样本克隆任意音色；三引擎自动择优（Fish 实时 / Qwen3 首包 ≈97ms 十语种 / VoxCPM 48kHz 可商用），多语种 TTS、实时变声、情感可调。",
    features: ["秒级零样本克隆", "三引擎自动择优", "10+ 语种合成", "实时变声 · 情感可调"],
    original: "原声样本",
    cloned: "克隆结果",
    langsLabel: "支持多语种",
    langs: ["中文", "English", "日本語", "한국어", "Español", "العربية", "Français", "Русский"],
  },
  deployDemo: {
    badge: "部署 · 自主可控私有",
    title: "自主可控 AI · 私有化部署",
    desc: "把大模型部署到你自己的服务器：数据不出本地、不上传公网，输出风格与行业知识库按业务自由微调，不依赖公有云策略，完全自主可控。",
    features: ["私有部署不出网", "数据主权自主可控", "行业知识库微调", "输出风格自定义"],
    cloudLabel: "公有云 API",
    localLabel: "你的私有部署",
    rows: [
      { label: "数据全程留在本地", cloud: false, local: true },
      { label: "不依赖公有云策略", cloud: false, local: true },
      { label: "无云端日志上报", cloud: false, local: true },
      { label: "可按业务微调", cloud: false, local: true },
      { label: "完全自主可控", cloud: false, local: true },
    ],
  },
  digitalDemo: {
    badge: "组合 · 高清活体分身",
    title: "高清活体数字人 / 虚拟主播",
    desc: "克隆形象 + 克隆声 + 口型同步的活体分身，会眨眼、会摆头、有微表情——非死图对口型。5090 上 25fps 高清、亚秒级首帧，一路直推 WebRTC / OBS，7×24 直播带货、批量产出口播视频。",
    features: ["活体形象 + 克隆声", "口型同步 · 会眨眼摆头", "WebRTC / OBS 直推", "亚秒级首帧 25fps"],
    tags: ["活体口型", "声音克隆", "多语配音", "虚拟背景"],
  },
  compare: {
    badge: "为什么选我们",
    title: "我们 vs 市面方案",
    subtitle: "同样是聚合聊天，差距在“会不会成交”。",
    cols: ["无界 AI", "普通聚合翻译软件", "纯人工团队"],
    rows: [
      { label: "翻译质量", us: "AI 拟人 · 地道口语/俚语", them: "谷歌式直译 · 生硬易错", manual: "看人，水平不一" },
      { label: "自动成交", us: "AI 主动促单 · 转化客户", them: "不支持", manual: "靠经验 · 易漏单" },
      { label: "人设语音", us: "文字转人设声音", them: "无", manual: "无" },
      { label: "7×24 在线", us: "全天候不漏客", them: "部分", manual: "受工时限制" },
      { label: "多平台聚合", us: "统一收件箱", them: "支持", manual: "手动切换" },
      { label: "私有部署 · 数据不出网", us: "支持", them: "多为云端", manual: "—" },
      { label: "规模化成本", us: "低 · 一人顶一队", them: "中", manual: "高 · 随人头涨" },
    ],
  },
  autochat: {
    badge: "旗舰业务 · AI 自动成交",
    title: "AI 自动成交聊天系统",
    subtitle:
      "不止聚合与翻译——AI 以你的人设，7×24 全自动接客、答疑、跟进、促单、转化客户，跨语言零障碍、真人级体验。市面软件多接谷歌等翻译 API，生硬易错、一眼穿帮；我们用 AI 翻译 + 对话技术，地道、拟人、会成交。",
    features: [
      { icon: "languages", title: "AI 拟人翻译", desc: "地道口语 + 地方俚语 + 文化语气，对方看不出你是外国人——告别谷歌式生硬直译。" },
      { icon: "bot", title: "AI 自动成交", desc: "懂上下文，主动答疑、跟进、引导下单、转化客户，7×24 不漏客，人工可随时接管。" },
      { icon: "mic", title: "人设语音聊天", desc: "AI 文字回复转成你的人设声音，发语音消息 / 语音聊天，更真实、更信任、更易成交。" },
      { icon: "inbox", title: "多平台聚合", desc: "TG / LINE / WhatsApp / Messenger 多号统一收件箱，规模化批量运营。" },
    ],
    compareTitle: "AI 拟人翻译 vs 普通翻译",
    compareNote: "同一句话，差距一眼可见——普通翻译让对方瞬间识破，AI 拟人翻译像本地人。",
    badLabel: "普通翻译 · 谷歌式",
    goodLabel: "我们 · AI 拟人翻译",
    compare: [
      {
        src: "¿Está disponible? lo quiero ya jaja",
        bad: "它可用吗？我现在要它哈哈",
        good: "还有货吗？我现在就想要哈哈～",
      },
      {
        src: "我们给你包邮，今天下单还送小礼物",
        bad: "We give you free shipping, today order also send small gift",
        good: "Free shipping on us — order today and grab a free gift 🎁",
      },
    ],
    scenariosLabel: "适用场景",
    scenarios: ["出海电商", "私域客服", "约单获客", "跨境社群"],
    cta: "Telegram 咨询 AI 成交方案",
    demo: {
      inbox: "统一收件箱 · AI 自动成交",
      personaName: "你的人设 AI",
      translatedTag: "AI 拟人翻译",
      autoTag: "自动成交",
      voiceTag: "人设语音",
      typing: "AI 正在以人设回复…",
      incoming: { name: "Carlos", flag: "🇪🇸", text: "¿Hacen envíos a México? precio?", translated: "发货到墨西哥吗？多少钱？" },
      reply: {
        text: "¡Claro! Envío a México en 5-7 días 🚀 Hoy con 10% OFF, ¿te lo aparto?",
        translated: "当然！墨西哥 5-7 天到 🚀 今天还有 9 折，要不要我先帮你留一份？",
      },
      voiceLen: "0:08 · 人设语音",
    },
  },
  engage: {
    badge: "合作方式 · 灵活共赢",
    title: "三种合作方式，总有一种适合你",
    subtitle: "无论你已有硬件、想省心全包，还是想投资共赢——我们都能落地。硬件归你所有，数据私有可控，结算方式灵活。",
    selectorTitle: "你的情况是？",
    youLabel: "你负责",
    weLabel: "我们负责",
    selector: [
      { id: "service", label: "我有硬件 · 找技术落地" },
      { id: "managed", label: "我要省心 · 全包托管" },
      { id: "invest", label: "我想投资 · 合作分红" },
    ],
    models: [
      {
        id: "service",
        badge: "最自主",
        name: "私有部署服务",
        tagline: "你的设备，我们负责落地",
        you: "自购硬件 · 提供场地",
        we: "选型建议 + 部署 + 定制 + 培训 + 支持",
        price: "咨询报价 · 按需定制",
        priceNote: "方案一对一评估 · 可加运维 198 / 月",
        points: ["数据 100% 私有、不出网", "按你的场景深度定制", "交付文档 + 上手培训", "7~30 天技术支持"],
        cta: "Telegram 咨询",
      },
      {
        id: "managed",
        badge: "最省心",
        name: "全托管 · 交钥匙",
        tagline: "硬件 + 机房 + 运维我们包，你只管用",
        you: "出需求 · 按月付费",
        we: "硬件代采 + 机房托管 + 部署 + 7×24 运维 + 升级",
        price: "from 1980 USD / 月",
        priceNote: "含机房 + 运维 · 硬件代采按成本另计",
        points: ["免运维、稳定在线", "7×24 监控与升级", "按需弹性扩容", "一价全包、省心无忧"],
        cta: "Telegram 咨询",
        highlight: true,
      },
      {
        id: "invest",
        badge: "最高回报",
        name: "机房投资合作分红",
        tagline: "你出资，我们专业运营，按月分红",
        you: "出资建节点 / 买卡",
        we: "技术落地 + 运营 + 获客 + 7×24 运维",
        price: "分红合作 · 起投 from 20,000 USD",
        priceNote: "净利分成 70 / 30（投资方占多数）",
        points: ["我们全程专业运营", "净利分成、你占多数", "风险共担、透明月结对账", "签约合作、权责清晰"],
        cta: "Telegram 洽谈合作",
      },
    ],
    serviceTiersLabel: "三档部署套餐（咨询报价）",
    extrasLabel: "更多可选服务",
    invest: {
      roiTitle: "示例测算 · 标准档 50,000 USD（满载估算）",
      roiRows: [
        { label: "预估月营收", value: "8,000 – 12,000 USD" },
        { label: "扣成本预估净利", value: "6,000 – 9,000 USD" },
        { label: "投资方月分红（70%）", value: "4,200 – 6,300 USD" },
        { label: "预估回本周期", value: "约 9 – 13 个月" },
      ],
      roiNote: "以上为满载理想估算，实际受利用率、市场与汇率波动影响，不构成任何收益承诺。",
      flowTitle: "合作流程",
      flow: ["洽谈评估", "签约 · 明确分成权责", "出资建节点（可代采）", "部署运营 · 月度分红对账"],
      compliance: "硬件归投资方所有；仅服务合法合规业务，遵守当地法律法规。",
    },
    matrixTitle: "三种方式对比",
    matrixCols: ["私有部署服务", "全托管交钥匙", "投资合作分红"],
    matrix: [
      { label: "硬件采购", a: "你自购", b: "我们代采", c: "你出资" },
      { label: "机房 / 场地", a: "你提供", b: "我们提供", c: "共建" },
      { label: "部署调试", a: "我们", b: "我们", c: "我们" },
      { label: "日常运维", a: "可选", b: "我们 7×24", c: "我们" },
      { label: "获客 / 接单", a: "—", b: "—", c: "我们" },
      { label: "计费方式", a: "一次性", b: "月费", c: "分红" },
      { label: "适合", a: "技术自主", b: "省心稳定", c: "投资收益" },
    ],
  },
  roi: {
    badge: "算一算 · 你能多赚多少",
    title: "AI 成交收益试算",
    subtitle: "拖动几个数字，估算 AI 自动成交每月能帮你省下的人力、多赚的营收。",
    inputs: { agents: "当前客服人数", salary: "单客服月薪", leads: "日均新咨询", aov: "客单价", conv: "当前转化率" },
    units: { agents: "人", salary: "USD", leads: "条/天", aov: "USD", conv: "%" },
    resultSaveLabel: "人力成本优化 / 月",
    resultRevenueLabel: "转化提升增收 / 月",
    resultNetLabel: "净增收益 / 月",
    resultRoiLabel: "投入产出比",
    resultYearLabel: "年化净增（估）",
    planLabel: "推荐套餐",
    perMonth: "/ 月",
    assumptionsTitle: "测算假设（可与客服按你的实际调整）",
    assumptions: [
      "AI 自动成交可优化约 60% 重复性人力成本",
      "拟人翻译 + 7×24 不漏客，转化率平均相对提升约 35%",
      "按每月 30 天、你输入的客单价与转化率估算",
    ],
    disclaimer: "以上为基于行业经验的估算模型，实际效果因行业、流量与运营而异，不构成任何收益承诺。",
    cta: "按我的数据要方案",
  },
  cases: {
    badge: "真实成果 · 看得见",
    title: "客户成果 & 多语种实战",
    subtitle: "从换脸直播到 AI 多语种自动成交——下面是不同场景的真实玩法与对话实录。",
    items: [
      { scene: "出海直播换脸", metric: "HD 25fps", metricLabel: "实时换脸直播", quote: "脸区原生通道清晰度肉眼可见，连麦全程稳，几乎看不出破绽。", name: "Aya", role: "全职主播", img: "/showcase/live-after.png" },
      { scene: "会议同传", metric: "术语零翻车", metricLabel: "克隆音同传", quote: "术语表锁定后专有名词再没翻错，用我自己的声音同传，客户以为我会外语。", name: "陈工", role: "跨境商务 · 负责人", img: "/showcase/digital-human.png" },
      { scene: "数字人口播", metric: "1 人顶一队", metricLabel: "批量起号", quote: "活体数字人会眨眼摆头，批量出口播视频，一个人把内容矩阵铺满。", name: "Mia", role: "MCN 工作室 · 主理人", img: "/showcase/live-before.png" },
    ],
    galleryTitle: "多语种 · 拟人成交实录",
    gallerySubtitle: "同一套 AI，地道接洽各国客户——翻译看不出外国人，回复主动促单。",
    translatedTag: "AI 拟人翻译",
    replyTag: "AI 自动成交",
    gallery: [
      { lang: "Español", flag: "🇪🇸", incoming: "¿Tienen envío a Chile? 🇨🇱", translated: "发货到智利吗？", reply: "¡Sí! Llega en 7-10 días 🚀 Hoy 10% OFF, ¿te lo aparto?" },
      { lang: "Português", flag: "🇧🇷", incoming: "Quanto custa? quero comprar agora", translated: "多少钱？我现在就想买", reply: "Sai por R$199 com frete grátis hoje 🎁 Reservo pra você?" },
      { lang: "العربية", flag: "🇸🇦", incoming: "هل المنتج متوفر؟", translated: "产品有货吗？", reply: "نعم متوفر ✅ شحن خلال ٥ أيام، وخصم ١٠٪ اليوم" },
      { lang: "ไทย", flag: "🇹🇭", incoming: "สนใจค่ะ ราคาเท่าไหร่", translated: "有兴趣，多少钱？", reply: "ราคา 990 บาท ส่งฟรีวันนี้ 🎉 รับเลยไหมคะ" },
    ],
    disclaimer: "案例数据来自客户反馈与内部统计，仅供参考；对话为多语种能力实录示意。",
  },
  lead: {
    title: "不方便现在开 Telegram？留个联系方式",
    subtitle: "留下需求，我们 5 分钟内主动联系你，按你的场景出方案与报价。",
    name: "称呼",
    contact: "联系方式",
    interest: "想了解",
    message: "需求备注（选填）",
    namePh: "怎么称呼你",
    contactPh: "Telegram / WhatsApp / 邮箱 / 微信",
    messagePh: "简单说说你的场景、平台或目标……",
    interests: ["AI 自动成交聊天", "聊天 / 同传翻译", "私有部署", "实时换脸换声（定制交付）", "整体托管（我们配硬件机房）", "投资合作分红", "其他"],
    submit: "提交，等客服联系我",
    submitting: "提交中…",
    successTitle: "已收到，马上联系你 ✅",
    successDesc: "我们会在 5 分钟内通过你留的方式联系你；急可直接点上方 Telegram。",
    error: "提交失败，请重试或直接联系 Telegram。",
    contactInvalid: "请填写有效的联系方式（Telegram / WhatsApp / 邮箱等）",
    privacy: "你的信息仅用于本次咨询，不对外共享。",
  },
  contact: {
    title: "联系我们 · 下单",
    subtitle: "添加 Telegram 客服，确认需求后即可开通。",
    telegram: "Telegram 客服",
    telegramHandle: "@WJKJ2026",
    scanHint: "手机扫码直达客服",
    usdt: "USDT 收款",
    usdtNote: "下单前请向客服核对最新收款地址，谨防诈骗。",
    networks: "支持 TRC20 / ERC20（推荐 TRC20，手续费低）。收款地址以客服当面确认为准。",
    responseTime: "客服响应 ≈ 5 分钟 · 7×24 在线",
    compliance: "合规与隐私",
    complianceNote: "仅限本人合法授权用途；禁止用于冒充、诈骗或侵犯他人权益。素材会话结束即删除，不长期留存。",
    cta: "联系 Telegram 客服",
  },
  footer: {
    rights: "无界科技 BOUNDLESS · 保留所有权利",
    disclaimerTitle: "授权与免责声明",
    disclaimer:
      "本平台服务仅供合法用途。用户须确保对所使用的肖像、声音及内容拥有完整授权，严禁用于诈骗、伪造、侵权或任何违法活动。使用即表示同意自行承担相应法律责任。",
    links: ["业务能力", "价格", "关于我们", "联系下单"],
  },
};

const en: Dict = {
  nav: {
    solutions: "Solutions",
    translate: "Translate",
    demo: "Live Swap",
    autochat: "AI Closing",
    cases: "Cases",
    engage: "Engagement",
    pricing: "Pricing",
    about: "About",
    contact: "Contact",
    cta: "Get Started",
  },
  hero: {
    badge: "BOUNDLESS · Cross-border chat AI · One inbox · Private deployment",
    title: "Chat with customers worldwide",
    titleAccent: "as naturally as with a neighbor",
    titleLines: ["Chat with customers worldwide", "as naturally as", "with a neighbor"],
    rotating: ["Human-like translation, 30+ languages", "AI follows up 24/7 in your persona", "Every platform, one inbox", "One-click human takeover", "Private deployment, data stays home"],
    subtitle:
      "Bring WhatsApp / Telegram / LINE / Messenger into one inbox. Customers write in any language — AI translates like a native, answers and follows up in your persona, and you take over with one click when it matters. Deploy privately; data never leaves your servers.",
    trustline: "24/7 self-healing production · 1100+ automated tests · Data stays on-prem",
    ctaPrimary: "Free consult · get a closing plan",
    ctaSecondary: "View plans & pricing",
    stats: [
      { value: "24H", label: "Watchdog-healed, always on" },
      { value: "30+", label: "Languages, human-like" },
      { value: "5", label: "Platforms, one inbox" },
      { value: "1100+", label: "Automated regression tests" },
    ],
  },
  solutionsSection: {
    title: "Three Families · Full Product Matrix",
    subtitle: "Growth (ChatX closing · ReachX lead-gen), Lingo (VoxX voice interpreting), Studio (VoiceX voice · LiveX digital twins) — three product families on one BOUNDLESS core, privately deployed with data on-prem, used alone or composed on demand.",
  },
  solutions: [
    {
      id: "chatx",
      tag: "ChatX",
      title: "AI Closing Chat · Unified Inbox",
      desc: "Every platform's messages flow into one workspace: AI answers, follows up and closes in your persona, human-like translation is built in, and you take over with one click at key moments; private deployment supported.",
      features: ["Unified multi-platform inbox", "Persona-driven AI follow-up", "Built-in human-like translation", "One-click human takeover"],
      highlight: true,
      pricing: [
        { plan: "Entry", price: "58 / mo", detail: "3 chat accounts · 1 platform", order: "autochat-entry" },
        { plan: "Team", price: "198 / mo", detail: "10 accounts · all platforms · AI auto-closing", order: "autochat-team" },
        { plan: "Flagship", price: "598 / mo", detail: "50 accounts · human takeover · analytics", order: "autochat-flagship" },
      ],
    },
    {
      id: "reach",
      tag: "ReachX",
      title: "Real-device Lead-gen · ReachX (Invite-only)",
      desc: "A real-device cluster for lead generation: multi-account management, group-member extraction and greeting outreach. It sits close to platform-compliance boundaries, so it is delivered privately after a per-team review — no public self-serve activation.",
      features: ["Real-device multi-account ops", "Bulk group-member extract", "Private delivery", "Invite-only review"],
      pricing: [
        { plan: "Private deployment", price: "Quote by scale", detail: "Real-device acquisition cluster, priced by device count & scale" },
      ],
    },
    {
      id: "voice",
      tag: "VoiceX",
      title: "Voice Cloning",
      desc: "Zero-shot clone from seconds of audio, with three engines auto-picked (Fish real-time / Qwen3 ≈97ms first-packet, 10 languages / VoxCPM 48kHz commercial), multilingual TTS, real-time voice change and emotion control.",
      features: ["Zero-shot cloning", "Tri-engine auto-pick", "10+ languages", "Real-time VC · emotion"],
      // 2026-08-04: voice capabilities fold into STUDIO five-tier membership (rows derived from TIERS).
      pricing: studioTierRows("en"),
    },
    {
      // 2026-08-04: LingoX merged into ChatX — this card is now ChatX's translate-plan card
      // (id / SKUs / deep links unchanged; lingox-* licensing keeps fulfilling as before).
      id: "translate",
      tag: "ChatX · Translate",
      title: "Cross-border Chat Translation · Translate Plans",
      desc: "Two-way real-time text + voice translation across platforms, glossary-locked proper nouns, cost-saving translation memory, and a unified inbox that builds customer assets — so teams that don't speak the language can chat with global clients on WhatsApp / Telegram / LINE. Translation ships inside the ChatX client, licensed by seats + character quota; download ChatX to use it.",
      features: ["Multi-platform translation", "Term lock · translation memory", "Unified inbox · customer assets", "Multimodal (image/voice)"],
      // Prices mirror lib/pricing.ts::translateOffers (USD; repriced 2026-07-18, competitor ×2); change both together.
      pricing: [
        { plan: "Char pack", price: "59", detail: "One-time · 1.5M chars + glossary + translation memory", order: "translate-charpack" },
        { plan: "Team", price: "99 / mo", detail: "3M chars/mo + multi-seat inbox + customer journey + funnel counter", order: "translate-team" },
        { plan: "Pro", price: "198 / mo", detail: "Unlimited chars + multimodal translate + confidence/engine health", order: "translate-pro" },
      ],
    },
    {
      id: "interpret",
      tag: "VoxX",
      title: "Cloned-voice Interpreting + Bilingual Subtitles",
      desc: "Two-way voice interpreting in your own cloned voice, a glossary that locks proper nouns, barge-in interruption; drop one URL into OBS for live bilingual subtitles, export SRT afterwards. Meetings, streams and co-calls covered. Currently delivered as a private beta: book a demo and we configure it per scenario.",
      features: ["Cloned-voice interpreting", "Term lock · barge-in", "OBS bilingual subtitles", "One-tap call package"],
      pricing: [
        { plan: "Beta booking", price: "Quote", detail: "Delivered after a meeting / live-stream scenario review" },
        { plan: "Self-hosted", price: "Quote", detail: "Buyout + annual support" },
      ],
    },
    {
      id: "private-ai",
      tag: "BOUNDLESS Engine",
      title: "Self-controlled AI · Private Deploy",
      desc: "Deploy LLMs on your own servers: data stays local, off the public cloud, with knowledge base and output style freely fine-tuned to your business — fully self-controlled.",
      features: ["Private, off the net", "Full data sovereignty", "Domain knowledge tuning", "Custom output style"],
      pricing: [
        { plan: "Cloud API", price: "≈1.2x token", detail: "Prepaid private relay, no retention" },
        { plan: "Single-node", price: "from 1600", detail: "One-time, incl. setup" },
        { plan: "Enterprise cluster", price: "from 6000", detail: "Quote by scale" },
        { plan: "Fine-tuning", price: "from 1000", detail: "Per task" },
      ],
    },
    {
      id: "digital-human",
      tag: "LiveX",
      title: "HD Living Digital Human / Virtual Streamer",
      desc: "A living twin — cloned face + cloned voice + lip-sync that blinks, turns its head and emotes, not a still photo. Streamed straight to WebRTC / OBS at 25fps HD with a sub-second first frame on flagship-tier hardware (subject to your deployment environment).",
      features: ["Living face + cloned voice", "Lip-sync · blinks & moves", "WebRTC / OBS stream", "Virtual bg · talking-heads"],
      // 2026-08-04: digital-human capabilities fold into STUDIO five-tier membership (rows derived from TIERS).
      pricing: studioTierRows("en"),
    },
    {
      id: "video-dubbing",
      tag: "LiveX",
      title: "AI Video Translation & Dubbing",
      desc: "Auto-translate videos, dub with cloned original voice, and align lip movements — built for global short video. Capabilities unlock with STUDIO membership; enterprise video-matrix is Flagship private deploy.",
      features: ["Auto subtitle translation", "Cloned voice dubbing", "Lip alignment", "Batch processing"],
      pricing: [
        { plan: "Pro+", price: tierPriceLabel(studioTier("pro"), "en"), detail: "Live swap · interpreting · see STUDIO", order: "pro" },
        { plan: "Flagship", price: tierPriceLabel(studioTier("flagship"), "en"), detail: "Private deploy · short-video matrix", order: "flagship" },
      ],
    },
  ],
  pricingSection: {
    note: `STUDIO: Free face-swap+watermark / Starter ${studioTier("starter").monthly} / Standard ${studioTier("standard").monthly} / Pro ${studioTier("pro").monthly} (monthly; quarterly & annual list prices) / Flagship quote. ChatX (closing + translate) keeps its own plans; anything beyond the list is custom by scenario.`,
  },
  trust: {
    platformsLabel: "Deeply integrated platforms",
    platformsLive: [
      { name: "Telegram", note: "Protocol-level · text / voice / media" },
      { name: "WhatsApp", note: "Two-way messaging · voice · media" },
      { name: "LINE", note: "Two-way messaging · media · welcome flows" },
      { name: "Messenger", note: "Web + mobile app, dual link" },
      { name: "Web", label: "Web Chat", note: "Embed on any site in minutes" },
      { name: "Facebook", note: "Real-device lead-gen · friends / groups" },
    ],
    platformsComingLabel: "More platforms · coming",
    platformsComing: ["Instagram", "TikTok", "X", "Discord", "WeChat", "Zalo", "Viber", "KakaoTalk", "Signal"],
    statsTitle: "Engineering facts, not adjectives",
    statsSubtitle: "Every number is backed by in-repo measurements and production records — verifiable and reproducible.",
    stats: [
      { value: "1100", suffix: "+", label: "Automated regression tests (both engines, verified)", sub: "Every change passes this net first" },
      { value: "50", suffix: "/50", label: "Back-translation eval, all passed (Aug 2026 weekly)", sub: "Dual-track: back-translation + semantics, re-run weekly" },
      { value: "22", suffix: "/22", label: "Cloud-outage drill, local model took over", sub: "Customers noticed nothing; circuit self-closed" },
      { value: "24", suffix: "/7", label: "In production · watchdog probes every 5 min", sub: "Crashes at 3am get back up on their own" },
      { value: "30", suffix: "+", label: "Languages, human-like translation (model scope)", sub: "Slang and tone that read like a local" },
      { value: "5", suffix: "", label: "Platforms into one inbox", sub: "Every channel lands in one workspace" },
      { value: "0.939", suffix: "", label: "Mean semantic score (out of 1.0)", sub: "Machine-judged, tracked weekly" },
      { value: "20", suffix: "+", label: "Dedicated quality-eval gates", sub: "Crisis safety / persona / media consistency…" },
    ],
    testimonialsTitle: "Why trust us",
    testimonials: [
      {
        quote: "ChatX runs in production 24/7: a watchdog probes every 5 minutes and pulls crashed instances back up, with restart cooldown gates and maintenance broadcasts — agents never notice.",
        name: "Production ops",
        role: "Source: deploy/instances records",
      },
      {
        quote: "Live-traffic cloud-outage drill: with the cloud LLM cut off, 22/22 requests got real answers from the local model — customers noticed nothing, and the circuit closed itself on recovery.",
        name: "Failover drill",
        role: "Source: Jul 2026 outage drill records",
      },
      {
        quote: "Translation ships behind dual-track back-translation + semantic evals: 50/50 passed on the wide corpus, 0.939 mean semantic score; weak language pairs switch to stronger engines on weekly batch data.",
        name: "Translation evals",
        role: "Source: translation_eval weekly batch (Aug 2026)",
      },
    ],
    disclaimer: "Figures above are internal engineering measurements and deployment records (Aug 2026), not a promise of specific business results.",
  },
  plans: {
    title: "AI Auto-Closing Chat · Plans",
    subtitle: "Aggregation + human-like AI translation + AI auto-closing + persona voice. Pick by account scale; save more annually.",
    monthly: "Monthly",
    yearly: "Yearly",
    save: "Save 15%",
    popular: "Most popular",
    perMonth: "/ mo",
    cta: "Choose plan",
    items: [
      {
        name: "Entry",
        priceMonthly: "58",
        priceYearly: "50",
        desc: "Small teams / individuals",
        features: ["3 chat accounts", "Human-like AI translation", "1 platform", "Basic voice cloning trial"],
        plan: "autochat-entry",
      },
      {
        name: "Team",
        priceMonthly: "198",
        priceYearly: "168",
        desc: "Best for growing teams",
        features: ["10 chat accounts", "All platforms unified", "AI auto-closing replies", "Persona voice messages", "Priority support"],
        highlight: true,
        plan: "autochat-team",
      },
      {
        name: "Flagship",
        priceMonthly: "598",
        priceYearly: "508",
        desc: "Scale / enterprise",
        features: ["50 chat accounts", "AI auto-closing + persona voice", "Human handoff + knowledge base", "Analytics dashboard", "Optional private deployment"],
        plan: "autochat-flagship",
      },
    ],
  },
  orderSteps: {
    title: "Get started in 3 steps",
    subtitle: "Standard plans are fully self-serve: order online, pay, and activation is automatic. Custom deals — just ping support.",
    steps: [
      { title: "Pick a plan", desc: "Choose your plan and billing period on the order page; for custom needs, Telegram support helps you decide." },
      { title: "Order & pay online", desc: "USDT / bank card accepted; the order page tracks payment progress live — bookmark it to check anytime." },
      { title: "Auto-activation", desc: "Once payment confirms, your license key / voucher is issued automatically — copy it right on the order page and activate." },
    ],
  },
  faq: {
    title: "FAQ",
    subtitle: "Still have questions? Reach our Telegram support directly.",
    items: [
      { q: "What payment methods do you accept?", a: "Standard plans are self-serve on the order page — pay in USDT (TRC20) and more, with automatic activation once payment lands. Large or enterprise deals can arrange alternative methods via our official Telegram support." },
      { q: "Do you support private deployment?", a: "Yes. Chat aggregation and self-controlled AI can both deploy to your own servers — data stays local with no cloud reporting." },
      { q: "How is your AI translation different from Google Translate?", a: "We use AI translation + chat tech that outputs native slang, local idioms and cultural tone — they can't tell you're foreign — unlike tools that wire up Google-style APIs and read stiff and literal." },
      { q: "Can AI close deals automatically? Can humans take over?", a: "Yes. AI works your persona 24/7 to engage, answer, follow up and convert; at key moments a human can take over in one click." },
      { q: "What does voice cloning need from me?", a: "Just a few dozen seconds of clear voice audio for zero-shot cloning; make sure you hold the rights to that voice." },
      { q: "How is a private LLM different from a public cloud API?", a: "A privately deployed LLM keeps data fully local, free of public-cloud dependencies — fine-tune its knowledge base and output style to your business, with no cloud reporting and full self-control." },
      { q: "Can I pay per usage?", a: "Yes. Most services offer both subscriptions and usage-based add-ons — pay for what you use, mix freely." },
      { q: "How do I know I'm talking to the official team and paying the right address?", a: "The payment address is shown live on the order page only, and our support uses only the official Telegram accounts listed on this site. If a \"support agent\" messages you first, or a third party forwards you an address, always go back to the order page and verify before acting." },
    ],
  },
  realtime: {
    badge: "Flagship · Deployment service",
    title: "Real-time Face + Voice Swap · Private Deployment Service",
    subtitle: "We deploy live-stream / video-call grade real-time face swap + voice cloning onto your own hardware and customize it to your scenario — you own the hardware, we handle spec advice, deployment, tuning, training and long-term support. Data stays fully private, off the public net, for any scenario.",
    videoNote: "Real-time swap demo · video coming soon",
    features: [
      { icon: "cpu", title: "Private · data in your racks", desc: "Runs on your own hardware — data never leaves; outputs carry a C2PA verifiable watermark by default." },
      { icon: "zap", title: "Native face channel · crisp", desc: "Faces run a native high-res channel, measured 4.5× sharper; 720p HD default, 1080p ultra on demand." },
      { icon: "monitor", title: "Virtual bg · two-in-frame", desc: "Live background / green-screen off-GPU; two people each swapped, auto-downshift under load, no stutter." },
      { icon: "shield", title: "Support · pre-broadcast check", desc: "3-second device check turns red before you go live; docs + training + ops, upgrades and remote help." },
    ],
    stepsTitle: "How we deliver",
    steps: [
      { title: "Consult & spec", desc: "Confirm your scenario, get a hardware spec list to purchase yourself." },
      { title: "Remote deploy", desc: "We install face swap / voice / digital human / private LLM on your device." },
      { title: "Tune to scenario", desc: "Optimize for your platform and workflow, with hands-on training." },
      { title: "Handover & support", desc: "Deliver docs and maintenance, with upgrades and tech support." },
    ],
    hardwareTitle: "Recommended hardware (you buy)",
    hardwareNote: "You purchase and fully own the hardware; we only advise and deploy — no compute resale. The runtime /api/hardware/guide endpoint returns per-feature tiers for your exact GPU.",
    hardware: [
      { tier: "Entry · swap/output", gpu: "RTX 5070 Ti / 4080 16G", use: "1080p live swap · batch image/video output" },
      { tier: "Pro · swap+human", gpu: "RTX 4090 24G / 5080", use: "1080p ultra swap · HD digital human · parallel scenarios" },
      { tier: "Flagship · all-in-one", gpu: "RTX 5090 32G (dual-ready)", use: "25fps HD living human + cloned voice + interpreting · multi-stream" },
    ],
    plansTitle: "Service packages & quotes",
    // 2026-08-04 repricing: private deployment is quoted by sales (STUDIO Flagship stance) —
    // no fixed one-time prices; self-serve buyers go to /order STUDIO plans (free face swap, paid from 39 USD/mo).
    plansNote: `Private deployment is custom-quoted by sales; standard capability is self-serve via STUDIO plans (free face swap to start, from ${STUDIO_PAID_FROM} USD/mo)`,
    availability: "3 deployment slots open this week · by reservation",
    plans: [
      {
        name: "Basic deploy",
        tag: "Single",
        price: "Contact for quote",
        unit: "custom-scoped · incl. setup",
        specs: ["Face swap OR voice, your pick", "Remote deploy + basic tuning", "Hands-on training", "7-day support"],
        cta: "Ask on Telegram",
      },
      {
        name: "Creator all-in",
        tag: "Popular",
        price: "Contact for quote",
        unit: "custom-scoped · incl. setup",
        specs: ["Face swap + voice + digital human", "Multi-scenario deep tuning", "Training + docs", "30-day support"],
        cta: "Ask on Telegram",
        highlight: true,
      },
      {
        name: "Everything",
        tag: "Full",
        price: "Contact for quote",
        unit: "custom-scoped · incl. setup",
        specs: ["Face + voice + digital human", "Self-controlled private LLM", "Full scenario tuning", "30-day support + 1mo ops"],
        cta: "Ask on Telegram",
      },
    ],
    extrasTitle: "More services",
    extras: [
      "Custom development · quoted per scope",
      "On-site deployment · quoted (incl. travel)",
      "Maintenance subscription · 198/mo or 1998/yr",
      "Per-session remote help · 160/hour",
    ],
    capacityNote: "You own the hardware and your data stays private, off the public net; we provide end-to-end service from selection to deployment, customization and ops — for any scenario.",
    cta: "Customize on Telegram",
  },
  faceswap: {
    badge: "Free demo · Image",
    title: "Image Face Swap · Free Demo",
    subtitle: "Want a taste first? Upload a photo and turn into a pro headshot, superhero, or astronaut. (For real-time, see the flagship service above.)",
    tabCustom: "Custom Target",
    tabTemplate: "Templates",
    uploadFace: "Your Photo",
    uploadFaceHint: "A clear, well-lit front-facing photo works best",
    uploadTarget: "Target Image",
    uploadTargetHint: "Your face will be swapped into this image",
    pickTemplate: "Pick a template",
    templates: [],
    consent: "I confirm I have the legal rights to the uploaded photo and will not use it for any unlawful purpose.",
    button: "Swap Face",
    processing: "Swapping… usually 30–60s, please wait",
    resultTitle: "Result",
    download: "Download",
    again: "Try another",
    privacy: "Images are used only for this swap and not stored long-term.",
    errConfig: "The face swap service is being configured. Stay tuned.",
    errNoConsent: "Please confirm the portrait authorization first.",
    errNoImage: "Please upload a photo first.",
    errSize: "Image too large. Please upload an image under 8MB.",
    errGeneric: "Swap failed. Please try a clearer front-facing photo.",
  },
  about: {
    title: "Why BOUNDLESS",
    subtitle: "Faces you can see, conversations you can feel — self-built tech, global-friendly, privately controllable.",
    points: [
      { title: "Self-built tech", desc: "Voice / face / conversation / interpreting engines built in-house — not thin wrappers, with numbers you can reproduce live." },
      { title: "Private control", desc: "Full local stack, data stays in your racks; outputs carry a C2PA verifiable watermark by default — built for regulated buyers." },
      { title: "Global-friendly", desc: "Flexible settlement, frictionless cross-border onboarding for global teams." },
      { title: "Fast delivery", desc: "Standard services run out of the box; custom needs get dedicated support, with training and ops included." },
    ],
  },
  community: {
    badge: "Community",
    title: "Follow the channel · Join the group",
    subtitle: "The channel posts real cases, new features and limited-time deals first; join the group to chat with peers and grab an AI auto-closing trial and exclusive perks.",
    perks: ["Real cases & results", "New features + discounts", "Group trial · peer chat"],
    cta: "Follow channel",
    groupCta: "Join group",
  },
  gate: {
    badge: "Members only",
    title: "Follow + join to unlock perks",
    subtitle: "Follow the official channel and join the group to unlock an exclusive discount code and a free trial slot.",
    joinChannel: "① Follow channel",
    joinGroup: "② Join group",
    joinedChannel: "✅ Channel followed",
    joinedGroup: "✅ Group joined",
    verify: "I'm done — verify & unlock",
    checking: "Checking…",
    webNote: "Open this page inside Telegram (Mini App) to auto-verify and unlock.",
    unlockedTitle: "🎉 Perks unlocked",
    unlockedDesc: "Show your code to support for a discounted quote and a reserved free-trial slot.",
    codeLabel: "Your exclusive code",
    code: "HL-VIP",
    cta: "Reserve trial · contact support",
    notYet: "No follow/join detected yet — please complete both steps, then verify.",
  },
  swap: {
    before: "Original",
    after: "Swapped",
    dragHint: "Drag to compare before / after",
    liveTag: "Live swapping",
    hudEngine: "FACE SWAP ENGINE",
    hudFps: "HD 25 FPS",
    hudLatency: "0.9s 1st frame",
    callStatus: "On call",
    you: "You · real",
    theySee: "They see",
    faceVoice: "Face + Voice",
    voiceCloning: "Cloning voice",
  },
  showcaseSection: {
    title: "Capabilities you can see",
    subtitle: "Not just claims — every core capability comes with a real, interactive demo.",
  },
  chatDemo: {
    badge: "Chat · Live translation",
    title: "Chat Aggregation + Live Translation",
    desc: "Unify TG / LINE / WhatsApp / Messenger accounts in one inbox with instant two-way translation, AI auto-reply and human handoff anytime.",
    features: ["Multi-platform inbox", "Two-way translation", "AI auto-reply", "Handoff + knowledge base"],
    translatedTag: "Translated",
    replyName: "AI Assistant",
    typing: "typing…",
    messages: [
      { name: "Carlos", flag: "🇪🇸", text: "¿Hacen envíos a México?", translated: "Do you ship to Mexico?" },
      { name: "Ayaka", flag: "🇯🇵", text: "在庫はまだありますか？", translated: "Is it still in stock?" },
    ],
    reply: { text: "Yes! We ship worldwide, 3–7 days.", translated: "Sent in the customer's language automatically." },
  },
  voiceDemo: {
    badge: "Voice · Tri-engine clone",
    title: "Voice Cloning",
    desc: "Zero-shot clone from a short clear sample; three engines auto-picked (Fish real-time / Qwen3 ≈97ms first-packet, 10 languages / VoxCPM 48kHz commercial), multilingual TTS, real-time voice change and emotion control.",
    features: ["Zero-shot cloning", "Tri-engine auto-pick", "10+ languages", "Real-time VC · emotion"],
    original: "Source sample",
    cloned: "Cloned result",
    langsLabel: "Multilingual",
    langs: ["中文", "English", "日本語", "한국어", "Español", "العربية", "Français", "Русский"],
  },
  deployDemo: {
    badge: "Deploy · Self-controlled private",
    title: "Self-controlled AI · Private Deploy",
    desc: "Deploy an LLM on your own servers: data never leaves, off the public internet, with knowledge base and output style freely fine-tuned to your business — no public-cloud dependency, fully self-controlled.",
    features: ["Private, off the net", "Full data sovereignty", "Domain knowledge tuning", "Custom output style"],
    cloudLabel: "Public Cloud API",
    localLabel: "Your private deploy",
    rows: [
      { label: "Data stays fully local", cloud: false, local: true },
      { label: "No public-cloud dependency", cloud: false, local: true },
      { label: "No cloud-side logging", cloud: false, local: true },
      { label: "Fine-tune to your business", cloud: false, local: true },
      { label: "Fully self-controlled", cloud: false, local: true },
    ],
  },
  digitalDemo: {
    badge: "Combo · HD living twin",
    title: "HD Living Digital Human / Virtual Streamer",
    desc: "A living twin — cloned face + cloned voice + lip-sync that blinks, turns and emotes, not a still photo. 25fps HD with a sub-second first frame on a 5090, streamed straight to WebRTC / OBS for 24/7 selling and batch talking-heads.",
    features: ["Living face + cloned voice", "Lip-sync · blinks & moves", "WebRTC / OBS stream", "Sub-second first frame 25fps"],
    tags: ["Living lip-sync", "Voice clone", "Multi-lang dub", "Virtual bg"],
  },
  compare: {
    badge: "Why us",
    title: "Us vs the rest",
    subtitle: "Same chat aggregation — the difference is whether it closes.",
    cols: ["BOUNDLESS AI", "Ordinary aggregator", "Pure human team"],
    rows: [
      { label: "Translation quality", us: "Human-like · slang & idioms", them: "Google-style literal · stiff", manual: "Varies by person" },
      { label: "Auto-closing", us: "AI pushes the sale & converts", them: "Not supported", manual: "Skill-based · misses leads" },
      { label: "Persona voice", us: "Text → persona voice", them: "None", manual: "None" },
      { label: "24/7 online", us: "Never misses a lead", them: "Partial", manual: "Limited by hours" },
      { label: "Multi-platform inbox", us: "Unified inbox", them: "Supported", manual: "Manual switching" },
      { label: "Private deploy · off the net", us: "Supported", them: "Mostly cloud", manual: "—" },
      { label: "Scaling cost", us: "Low · 1 person = a team", them: "Medium", manual: "High · grows with headcount" },
    ],
  },
  autochat: {
    badge: "Flagship · AI auto-closing",
    title: "AI Auto-Closing Chat System",
    subtitle:
      "Beyond aggregation and translation — AI works your persona to greet, answer, follow up, push the sale and convert customers 24/7, across languages, at human-level quality. Most tools wire up Google-style translation APIs that read stiff and blow your cover; we use AI translation + chat that sounds native and actually closes.",
    features: [
      { icon: "languages", title: "Human-like AI translation", desc: "Native slang, local idioms and cultural tone — they can't tell you're foreign. No more stiff, literal Google output." },
      { icon: "bot", title: "AI that closes", desc: "Context-aware: answers, follows up, guides the order and converts — 24/7, never misses a lead, humans can take over anytime." },
      { icon: "mic", title: "Persona voice chat", desc: "Turn AI replies into your persona's voice — voice messages and voice chat that feel real, build trust and close faster." },
      { icon: "inbox", title: "Multi-platform inbox", desc: "TG / LINE / WhatsApp / Messenger, many accounts in one inbox — operate at scale." },
    ],
    compareTitle: "Human-like AI translation vs ordinary translation",
    compareNote: "Same sentence, obvious gap — ordinary translation gives you away; ours reads like a local.",
    badLabel: "Ordinary · Google-style",
    goodLabel: "Ours · AI human-like",
    compare: [
      {
        src: "¿Está disponible? lo quiero ya jaja",
        bad: "Is it available? I want it now haha",
        good: "Still in stock? I want one right now lol",
      },
      {
        src: "我们给你包邮，今天下单还送小礼物",
        bad: "We give you free shipping, today order also send small gift",
        good: "Free shipping on us — order today and grab a free gift 🎁",
      },
    ],
    scenariosLabel: "Use cases",
    scenarios: ["Cross-border e-com", "Private-domain support", "Lead closing", "Global communities"],
    cta: "Ask about AI closing on Telegram",
    demo: {
      inbox: "Unified inbox · AI auto-closing",
      personaName: "Your persona AI",
      translatedTag: "AI human-like",
      autoTag: "Auto-close",
      voiceTag: "Persona voice",
      typing: "AI replying in persona…",
      incoming: { name: "Carlos", flag: "🇪🇸", text: "¿Hacen envíos a México? precio?", translated: "Do you ship to Mexico? Price?" },
      reply: {
        text: "¡Claro! Envío a México en 5-7 días 🚀 Hoy con 10% OFF, ¿te lo aparto?",
        translated: "Sure! 5-7 days to Mexico 🚀 10% off today — want me to reserve one for you?",
      },
      voiceLen: "0:08 · persona voice",
    },
  },
  engage: {
    badge: "Engagement · flexible & win-win",
    title: "Three ways to work with us",
    subtitle: "Whether you already own hardware, want a fully managed setup, or want to invest and share returns — we deliver. You own the hardware, data stays private and controlled, with flexible settlement.",
    selectorTitle: "Which fits you?",
    youLabel: "You handle",
    weLabel: "We handle",
    selector: [
      { id: "service", label: "I have hardware · need delivery" },
      { id: "managed", label: "I want it fully managed" },
      { id: "invest", label: "I want to invest · share returns" },
    ],
    models: [
      {
        id: "service",
        badge: "Most control",
        name: "Private Deployment Service",
        tagline: "Your hardware, we make it work",
        you: "Buy hardware · provide space",
        we: "Spec advice + deploy + customize + train + support",
        price: "Contact for quote",
        priceNote: "scoped one-on-one · ops add-on 198 / mo",
        points: ["100% private, off the net", "Deeply tailored to your scenario", "Docs + hands-on training", "7–30 days tech support"],
        cta: "Ask on Telegram",
      },
      {
        id: "managed",
        badge: "Most hassle-free",
        name: "Turnkey · Fully Managed",
        tagline: "Hardware + datacenter + ops on us, you just use it",
        you: "Bring needs · pay monthly",
        we: "Hardware procurement + hosting + deploy + 24/7 ops + upgrades",
        price: "from 1980 USD / mo",
        priceNote: "incl. hosting + ops · hardware at cost",
        points: ["Zero ops, always online", "24/7 monitoring & upgrades", "Elastic scaling on demand", "All-in price, worry-free"],
        cta: "Ask on Telegram",
        highlight: true,
      },
      {
        id: "invest",
        badge: "Highest return",
        name: "Datacenter Investment & Revenue Share",
        tagline: "You invest, we operate, monthly dividends",
        you: "Fund nodes / buy GPUs",
        we: "Delivery + operations + client acquisition + 24/7 ops",
        price: "revenue share · from 20,000 USD",
        priceNote: "net profit split 70 / 30 (investor majority)",
        points: ["We operate it end-to-end", "Net profit split in your favor", "Shared risk, transparent monthly settlement", "Contract-based, clear responsibilities"],
        cta: "Discuss on Telegram",
      },
    ],
    serviceTiersLabel: "Three deploy packages (quoted)",
    extrasLabel: "More optional services",
    invest: {
      roiTitle: "Example · standard 50,000 USD (full-load estimate)",
      roiRows: [
        { label: "Est. monthly revenue", value: "8,000 – 12,000 USD" },
        { label: "Est. net profit after cost", value: "6,000 – 9,000 USD" },
        { label: "Investor monthly share (70%)", value: "4,200 – 6,300 USD" },
        { label: "Est. payback period", value: "~9 – 13 months" },
      ],
      roiNote: "Ideal full-load estimate; actual results depend on utilization, market and FX — not a guarantee of returns.",
      flowTitle: "How it works",
      flow: ["Discuss & assess", "Sign · define split & duties", "Fund & build nodes (we can procure)", "Operate · monthly dividend settlement"],
      compliance: "Hardware belongs to the investor; lawful, compliant use only, per local regulations.",
    },
    matrixTitle: "Compare the three",
    matrixCols: ["Deployment Service", "Turnkey Managed", "Investment Share"],
    matrix: [
      { label: "Hardware purchase", a: "You", b: "We procure", c: "You fund" },
      { label: "Datacenter / space", a: "You", b: "We", c: "Co-built" },
      { label: "Deploy & tune", a: "We", b: "We", c: "We" },
      { label: "Day-to-day ops", a: "Optional", b: "We 24/7", c: "We" },
      { label: "Client acquisition", a: "—", b: "—", c: "We" },
      { label: "Billing", a: "One-time", b: "Monthly", c: "Revenue share" },
      { label: "Best for", a: "Self-reliant", b: "Hassle-free", c: "Investment return" },
    ],
  },
  roi: {
    badge: "Calculate · how much more you'd earn",
    title: "AI Closing ROI Calculator",
    subtitle: "Drag a few numbers to estimate the labor you save and revenue you gain each month with AI auto-closing.",
    inputs: { agents: "Current support agents", salary: "Salary per agent", leads: "Daily new inquiries", aov: "Avg order value", conv: "Current conversion" },
    units: { agents: "ppl", salary: "USD", leads: "/day", aov: "USD", conv: "%" },
    resultSaveLabel: "Labor cost saved / mo",
    resultRevenueLabel: "Conversion uplift / mo",
    resultNetLabel: "Net gain / mo",
    resultRoiLabel: "Return on spend",
    resultYearLabel: "Annualized net (est.)",
    planLabel: "Suggested plan",
    perMonth: "/ mo",
    assumptionsTitle: "Assumptions (tune with support to your reality)",
    assumptions: [
      "AI auto-closing optimizes ~60% of repetitive labor cost",
      "Human-like translation + 24/7 lifts conversion by ~35% relative",
      "Estimated over 30 days using your AOV and conversion",
    ],
    disclaimer: "An estimate model based on industry experience; actual results vary by industry, traffic and operations — not a guarantee of returns.",
    cta: "Get a plan with my numbers",
  },
  cases: {
    badge: "Real results · see it",
    title: "Client results & multi-language in action",
    subtitle: "From live-stream face swap to AI multi-language auto-closing — real plays and chat logs across scenarios.",
    items: [
      { scene: "Live-stream face swap", metric: "HD 25fps", metricLabel: "Real-time swap", quote: "The native face channel is visibly sharper — solid through the whole co-stream, barely a tell.", name: "Aya", role: "Full-time streamer", img: "/showcase/live-after.png" },
      { scene: "Meeting interpreting", metric: "0 term errors", metricLabel: "Cloned-voice interpret", quote: "The glossary locked our proper nouns; interpreting in my own voice, clients thought I spoke the language.", name: "Mr. Chen", role: "Cross-border biz · Lead", img: "/showcase/digital-human.png" },
      { scene: "Digital-human videos", metric: "1 = a team", metricLabel: "Batch content", quote: "The living human blinks and moves; batching talking-heads let one person fill the whole content matrix.", name: "Mia", role: "MCN Studio · Founder", img: "/showcase/live-before.png" },
    ],
    galleryTitle: "Multi-language · human-like closing logs",
    gallerySubtitle: "One AI engaging customers worldwide — translation that hides your origin, replies that push the sale.",
    translatedTag: "AI human-like",
    replyTag: "AI auto-close",
    gallery: [
      { lang: "Español", flag: "🇪🇸", incoming: "¿Tienen envío a Chile? 🇨🇱", translated: "Do you ship to Chile?", reply: "¡Sí! Llega en 7-10 días 🚀 Hoy 10% OFF, ¿te lo aparto?" },
      { lang: "Português", flag: "🇧🇷", incoming: "Quanto custa? quero comprar agora", translated: "How much? I want to buy now", reply: "Sai por R$199 com frete grátis hoje 🎁 Reservo pra você?" },
      { lang: "العربية", flag: "🇸🇦", incoming: "هل المنتج متوفر؟", translated: "Is the product available?", reply: "نعم متوفر ✅ شحن خلال ٥ أيام، وخصم ١٠٪ اليوم" },
      { lang: "ไทย", flag: "🇹🇭", incoming: "สนใจค่ะ ราคาเท่าไหร่", translated: "Interested, how much?", reply: "ราคา 990 บาท ส่งฟรีวันนี้ 🎉 รับเลยไหมคะ" },
    ],
    disclaimer: "Case figures are from client feedback and internal stats, for reference; chats are illustrative of multi-language capability.",
  },
  lead: {
    title: "Not ready to open Telegram? Leave your contact",
    subtitle: "Drop your needs and we'll reach out within 5 minutes with a plan and quote for your scenario.",
    name: "Name",
    contact: "Contact",
    interest: "Interested in",
    message: "Notes (optional)",
    namePh: "What should we call you",
    contactPh: "Telegram / WhatsApp / email",
    messagePh: "Briefly describe your scenario, platform or goal…",
    interests: ["AI auto-closing chat", "Chat / interpreting translation", "Private deployment", "Real-time face & voice swap (custom delivery)", "Turnkey (we supply hardware & DC)", "Investment / revenue share", "Other"],
    submit: "Submit — have support reach me",
    submitting: "Submitting…",
    successTitle: "Got it — reaching out shortly ✅",
    successDesc: "We'll contact you within 5 minutes via what you left; for urgent needs tap Telegram above.",
    error: "Submit failed, please retry or contact us on Telegram.",
    contactInvalid: "Please enter a valid contact (Telegram / WhatsApp / email).",
    privacy: "Your info is used only for this inquiry and never shared.",
  },
  contact: {
    title: "Contact & Order",
    subtitle: "Add our Telegram support, confirm your needs, and get started.",
    telegram: "Telegram Support",
    telegramHandle: "@WJKJ2026",
    scanHint: "Scan to chat on mobile",
    usdt: "USDT Payment",
    usdtNote: "Always verify the latest receiving address with support before paying. Beware of scams.",
    networks: "TRC20 / ERC20 supported (TRC20 recommended, low fees). Confirm the address with support before paying.",
    responseTime: "Support replies in ~5 min · online 24/7",
    compliance: "Compliance & Privacy",
    complianceNote: "For your own lawfully authorized use only; no impersonation, fraud or infringement. Materials are deleted after the session, not stored long-term.",
    cta: "Contact on Telegram",
  },
  footer: {
    rights: "BOUNDLESS · All rights reserved",
    disclaimerTitle: "Authorization & Disclaimer",
    disclaimer:
      "Services are for lawful use only. Users must hold full rights to any likeness, voice or content used, and must not use the services for fraud, forgery, infringement or any illegal activity. Use implies acceptance of full legal responsibility.",
    links: ["Solutions", "Pricing", "About", "Contact"],
  },
};

export const content: Record<Lang, Dict> = { zh, en };
