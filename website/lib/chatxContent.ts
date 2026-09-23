/**
 * 智聊 ChatX 桌面客户端下载页内容单一数据源（/download/chatx 与 /en/download/chatx 共用）。
 *
 * 与幻境 STUDIO（manualContent.ts，原 AvatarHub）/ 智控（matrixxContent.ts）刻意分离：三个客户端的
 * 上手卡点完全不同——ChatX 是「聚合 AI 聊天工作台」桌面形态：不需要显卡（AI 推理走
 * 云端/内网服务），最大卡点是首启初始化（内嵌本地服务解包）与未签名安装包的
 * SmartScreen 提示，本页重点覆盖这两处。
 *
 * 发布新版本时：更新 download.version / size / filename / sha256；页面运行时还会
 * fetch /downloads/manifest.json（打包脚本生成）自动校正版本与哈希，此处是构建时兜底。
 */
import type { BrandLang } from "./brand";
import { faqPageJsonLd, plainText } from "./jsonld";

export interface Cx<T = string> {
  zh: T;
  en: T;
}

/** 下载源基址：与 desktop/package.json 的 electron-updater publish url 指向同一目录
 *  （同源，官网下载版与客户端自动更新版一致）。**这是对外 clean 包渠道**：`latest.yml` /
 *  `manifest.json` / `announcements.json` 都在这一层，本页与自动更新只认它。 */
export const CHATX_RELEASE_BASE = "/downloads";

/** 内测渠道指针（L-5 / 老板决策 D-L1，2026-09-06）：smart 包（随包 seed-data 的内测形态）
 *  的自动更新源在 `/downloads/internal/`，更新清单叫 `latest-internal.yml`（electron-builder
 *  `publish.channel=latest-internal` 烘进包内 `resources/app-update.yml`，由 `npm run dist:win`
 *  出包、`website/scripts/publish_chatx.ps1 -Channel internal` 上架）。
 *  **刻意不在任何页面渲染、不进 JSON-LD、不进 manifest**：它不是客户下载物；写在这里只为
 *  把两条渠道的布局钉在同一个事实源旁边，改路径时两边一起改（`/dl/downloads/internal/...`
 *  同样走 R2 分流，见 lib/mirror.ts DL_PREFIXES）。 */
export const CHATX_INTERNAL_RELEASE_BASE = "/downloads/internal";
export const CHATX_INTERNAL_UPDATE_MANIFEST = `${CHATX_INTERNAL_RELEASE_BASE}/latest-internal.yml`;

/** lite 定制档渠道（2026-09-06 老板「需要拍板的按建议做」）：`npm run dist:win:lite` 出的包
 *  烘入 `publish.channel=latest-lite`，自动更新只在 `/downloads/lite/` 内走——此前 lite 跟公共
 *  渠道，客户点更新会被换成 clean 包。同样不渲染、不进 JSON-LD/manifest。 */
export const CHATX_LITE_RELEASE_BASE = "/downloads/lite";
export const CHATX_LITE_UPDATE_MANIFEST = `${CHATX_LITE_RELEASE_BASE}/latest-lite.yml`;

export const CHATX = {
  download: {
    // ⚠ 这几个字段不只是页面兜底文案：它们同时进 SoftwareApplication JSON-LD（见本文件末
    // chatxDownloadJsonLd），而爬虫**不会**执行运行时那次 manifest.json 校正——落后的版本号
    // 会被 AI 当事实引用。2026-09-02 随 1.0.70 发版同步（发版流程：改这里四个值
    // version/size/filename/sha256，与 /downloads/manifest.json 对齐，gate:content 检查 5 钉住）。
    // 2026-09-04 随 1.0.73 发版同步；2026-09-06 随 1.0.74 发版同步（K-5 ③）；2026-09-06 随 1.0.75 发版同步（L-7 E）；2026-09-07 随 1.0.76 发版同步（M-6 C）；2026-09-08 随 1.0.77 发版同步（N-5 E）；2026-09-08 随 1.0.78 发版同步（R78）；2026-09-10 随 1.0.79 发版同步（R79）；2026-09-11 随 1.0.80 发版同步（R80）；2026-09-11 随 1.0.81 发版同步（R81，单一版本）；2026-09-11 随 1.0.82 发版同步（R82）；2026-09-12 随 1.0.84 发版同步（R84）；2026-09-13 随 1.0.85 发版同步（R85）；2026-09-16 随 1.0.86 发版同步（R86）；2026-09-17 随 1.0.87 发版同步（R87）；2026-09-18 随 1.0.88 发版同步（R88）；2026-09-18 随 1.0.89 发版同步（R89）。
    version: "1.0.97",
    size: { zh: "470 MB", en: "470 MB" },
    filename: "ChatX-Setup-1.0.97.exe",
    os: { zh: "Windows 10 / 11（64 位）", en: "Windows 10 / 11 (x64)" },
    url: `${CHATX_RELEASE_BASE}/ChatX-Setup-1.0.97.exe`,
    /** 与实际上架 /downloads/ 的安装包一致（scripts/gen-chatx-manifest.ps1 计算）；
     *  运行时会被 manifest.json 的值覆盖。 */
    sha256: "1b5dcc95a2b71aa9b3022c8b33333ebdcb376591f864c80ba000ef38e216f53f",
    /** 运行时清单（打包脚本生成，含 version/size/sha256/signed）。 */
    manifestUrl: `${CHATX_RELEASE_BASE}/manifest.json`,
    macNote: {
      zh: "macOS 版规划中：当前后端为 Windows 原生打包，跨平台需分别构建。",
      en: "A macOS build is planned: the bundled backend is currently packaged natively for Windows.",
    },
  },
  preCheck: [
    { zh: "系统：Windows 10 / 11（64 位）", en: "OS: Windows 10 / 11 (64-bit)" },
    { zh: "显卡：无需独立显卡（AI 推理在云端 / 内网服务完成）", en: "GPU: none required (AI inference runs on cloud / LAN services)" },
    { zh: "磁盘：预留约 1.5 GB 空间（安装 + 本地数据）", en: "Disk: keep ~1.5 GB free (install + local data)" },
    { zh: "网络：需能访问所接入的聊天平台与 AI 服务", en: "Network: access to your chat platforms and AI services" },
  ],
  install: {
    steps: [
      {
        title: { zh: "下载安装包", en: "Download the installer" },
        time: { zh: "约 2–5 分钟", en: "~2–5 min" },
        // 刻意不在这里重复版本号与体积：上面 download 段已是唯一事实源，正文再抄一份
        //（历史上抄成了「1.001.exe / 453 MB」）就会各自腐烂，且这段文字会被爬虫当事实引用。
        detail: {
          zh: "点击本页下载按钮获取安装包（版本号与体积见按钮旁标注；含桌面壳与本地服务，一次到位无需再下组件）。",
          en: "Click the download button on this page to get the installer (version and size are shown next to the button; desktop shell plus local backend in one package — no extra components).",
        },
      },
      {
        title: { zh: "运行安装程序", en: "Run the installer" },
        time: { zh: "约 1 分钟", en: "~1 min" },
        detail: {
          zh: "双击安装包，一路点 **下一步** 即可，支持自选安装目录。完成后桌面与开始菜单出现「智聊 ChatX」图标。",
          en: "Double-click the installer and click **Next** through the wizard; choose an install folder if you like. A ChatX icon appears on the desktop and Start menu.",
        },
        warn: {
          zh: "**蓝底 SmartScreen**「Windows 已保护你的电脑」：这是对新发布程序的例行信誉提醒，不是病毒报警。点 **更多信息 → 仍要运行** 即可继续（安装向导首页也会再次提示）。建议先用本页 **SHA-256** 核对安装包。",
          en: "**Blue SmartScreen** \"Windows protected your PC\" is a routine reputation notice for newly published apps — not a virus alert. Click **More info → Run anyway** to continue (the installer repeats this tip). Verify the **SHA-256** on this page first.",
        },
        blocker: {
          zh: "红底 **「已被应用程序控制策略阻止」**（Smart App Control 强制模式）：属系统策略硬拦截，安装界面**无法跳过**。请换一台未强制开启 SAC 的电脑安装，或联系客服获取已签名安装包 / 远程协助。",
          en: "Red **\"Application Control policy has blocked this file\"** (Smart App Control enforced) is a hard OS policy block — it **cannot be skipped** in the installer. Install on a PC without SAC enforced, or contact support for a signed build / remote assistance.",
        },
      },
      {
        title: { zh: "首次启动 · 本地服务初始化", en: "First launch · local service init" },
        time: { zh: "约 10–30 秒", en: "~10–30 s" },
        detail: {
          zh: "首次打开会解包并启动内嵌的本地服务，出现工作台登录页即就绪；之后每次启动都是秒级。数据保存在本机用户目录，卸载重装不丢配置。",
          en: "The first launch unpacks and starts the bundled local service; you're ready when the workspace login page appears. Subsequent launches are instant. Data lives in your local user folder and survives reinstalls.",
        },
      },
      {
        title: { zh: "登录工作台并接入渠道", en: "Sign in and connect channels" },
        time: { zh: "约 5–10 分钟", en: "~5–10 min" },
        detail: {
          zh: "用默认管理员账号登录后，在「渠道中心」接入你的聊天账号（Telegram / WhatsApp / Messenger / LINE 等）。**无需填写任何云 API Key**——翻译与智能回复经官网安全通道自动可用。",
          en: "Sign in with the default admin account and connect chat accounts in Channel Center (Telegram / WhatsApp / Messenger / LINE and more). **No cloud API key to enter** — translation and smart replies work via our secure gateway.",
        },
      },
      {
        title: { zh: "开始接待", en: "Start closing" },
        time: { zh: "即刻", en: "Now" },
        detail: {
          zh: "统一收件箱汇聚全渠道消息：AI 自动拟稿 / 自动回复、实时互译、语音消息、客户画像与跟进提醒全部就位。",
          en: "The unified inbox aggregates every channel: AI drafting / auto-reply, live translation, voice messages, customer profiles and follow-up reminders — all ready to go.",
        },
      },
    ],
    faqs: [
      {
        q: { zh: "SmartScreen / 杀毒软件提示风险，安全吗？", en: "SmartScreen / antivirus flags the installer — is it safe?" },
        a: {
          zh: "安装包为新发布的未签名构建，Windows 与部分杀软会对「首次见到的程序」例行提示，属于信誉机制而非病毒检出。请只从本官网下载，并用页面公布的 **SHA-256** 校验（PowerShell：`Get-FileHash 安装包路径`），哈希一致即为官方原版。**蓝底**可点「更多信息 → 仍要运行」继续；**红底 Smart App Control 强制拦截**无法在安装时跳过，需签名包或调整系统策略。代码签名证书接入后此类提示会逐步消失。",
          en: "The installer is a newly published, unsigned build. Windows and antivirus tools routinely warn about first-seen programs — reputation, not a virus hit. Download only here and verify **SHA-256**. **Blue SmartScreen**: More info → Run anyway. **Red Smart App Control block** cannot be skipped at install time — need a signed build or a PC without SAC enforced. Signing will phase these warnings out.",
        },
      },
      {
        q: { zh: "需要自己配置 AI 的 API Key 吗？", en: "Do I need my own AI API key?" },
        a: {
          zh: "不需要。智聊托管版启动后会经官网验证本机并走我们的云端 AI 通道，**Key 只在服务端**，工作台里看不到、也无需填写。AI 用量按 Token 计量（标准翻译免费、不耗 Token）；余额与流水在会员中心随时可查，用尽可自助购买 Token 包，也不会断线（自动降级到免费引擎）。",
          en: "No. Managed ChatX verifies this PC with our site and routes AI through our cloud gateway — **the vendor key never leaves the server**. You don't enter or manage keys. AI usage meters in tokens (standard translation is free and costs none); check balance and ledger in the membership center, top up with packs anytime — and you never go offline (graceful fallback to free engines).",
        },
      },
      {
        q: { zh: "以后怎么升级？要重新下载吗？", en: "How do updates work? Do I re-download?" },
        a: {
          zh: "客户端内置自动更新：启动时检查新版本，后台静默下载，退出时自动安装，账号与数据全保留，无需手动重装。",
          en: "Auto-update is built in: the client checks on launch, downloads in the background and installs on exit — accounts and data preserved, no manual reinstall.",
        },
      },
      {
        // 2026-08-28 口径统一：本条原文写「不经过我们的服务器中转…AI 回复调用你自己配置的
        // AI 服务」，那是**自带 Key 时代的旧口径**，与上面「Do I need my own AI API key?」
        // 那条（托管版走我们的云网关、Key 在服务端）直接打架。两句同页并存，恰好砸在
        // 「数据主权」这个核心卖点上；而目录站（G2/Capterra/AlternativeTo）会把这段原样
        // 镜像到几十个站点，改回来极难。现按事实拆成两件事：**聊天数据**留本机（真），
        // **AI 推理**默认走托管网关、可改指向自有端点或内网模型节点（也真——
        // hosted_gateway.py + 官网 /api/ai/* 是前者，config 的 base_url 是后者）。
        q: { zh: "我的账号和聊天数据存在哪里？", en: "Where do my accounts and chat data live?" },
        a: {
          zh: "聊天记录、登录会话、客户资料全部保存在**本机用户目录**，不经我们的服务器中转。**AI 推理是另一回事**：托管版默认把推理请求送到我们的云端 AI 网关（所以你不必自备也看不到 Key），也可以在配置里改指向你自己的端点或内网模型节点——两种模式下聊天数据都不离开你的机器。",
          en: "Chat history, platform sessions and customer profiles stay in **your local user folder** — they are not routed through our servers. **AI inference is the exception**: managed ChatX sends inference requests to our cloud AI gateway by default (which is why you never handle an API key), and it can be pointed at your own endpoint or an on-prem model node instead. Either way the chat data itself never leaves your machine.",
        },
      },
      {
        q: { zh: "支持哪些聊天平台？", en: "Which chat platforms are supported?" },
        a: {
          zh: "Telegram、WhatsApp、Messenger、LINE 等主流平台的消息可汇聚到统一收件箱；各平台的接入方式（协议 / 网页会话）在渠道中心里按向导完成。",
          en: "Telegram, WhatsApp, Messenger, LINE and more aggregate into the unified inbox; each platform's onboarding (protocol / web session) is wizard-guided in Channel Center.",
        },
      },
      {
        q: { zh: "需要显卡或很强的电脑吗？", en: "Do I need a GPU or a powerful machine?" },
        a: {
          zh: "不需要。AI 推理在云端或你的内网算力节点完成，本机只跑工作台与本地服务，普通办公电脑即可流畅使用。",
          en: "No. AI inference runs on cloud or your LAN compute nodes; the desktop only runs the workspace and a lightweight local service — a regular office PC is plenty.",
        },
      },
      {
        // 2026-08-19 Token 定价改版：免费版=正式档位（非限时试用），试用口径全站唯一
        //（注册送 10,000 体验 Token，与 /pricing、下单页同源），不再引导「找顾问拿试用码」。
        q: { zh: "免费吗？怎么获得正式授权？", en: "Is it free? How do I get a license?" },
        a: {
          zh: "下载即免费开始（非限时试用）：标准翻译永久免费不限字符 + 每月 1,000 Token，注册再送 10,000 体验 Token。要更多 AI 用量直接充值（50U 起、1U = 1,500 Token、首充最高 +40%；新人 6U 大礼包双倍到账），到账自动开通，无需找顾问。",
          en: "The download is the free start (not a timed trial): unlimited standard translation forever plus 1,000 tokens/mo, with 10,000 bonus tokens on signup. Need more AI usage? Just top up (from 50U at 1U = 1,500 tokens, up to +40% on your first top-up; newcomer 6U pack at double rate) — auto-activation, no sales call required.",
        },
      },
    ],
  },
} as const;

/** 便捷取值：按语言取一段文案。 */
export function cx(field: Cx, lang: BrandLang): string {
  return field[lang];
}

/** 下载页结构化数据（实施77 GEO 批次3，zh/en 两页共用一份构造，防口径分叉）。
 *
 *  返回 [SoftwareApplication, FAQPage] 两个节点：
 *  - SoftwareApplication 的每个字段都必须对应页面上真实可见的事实（版本/体积/系统要求/免费），
 *    **绝不挂 aggregateRating**——没有真实评分数据，编一个就是给 AI 喂假话（也违反 schema 政策）；
 *  - FAQPage 与页面折叠面板同源（CHATX.install.faqs），Markdown 标记经 plainText 剥净。
 *
 *  publisher 用 @id 指回根 layout 的 Organization 节点，让「这个软件属于哪个实体」在图谱里连通。 */
export function chatxDownloadJsonLd(lang: BrandLang, siteUrl: string): object[] {
  const zh = lang === "zh";
  const d = CHATX.download;
  const app = {
    "@context": "https://schema.org",
    "@type": "SoftwareApplication",
    "@id": `${siteUrl}${zh ? "" : "/en"}/download/chatx#software`,
    name: zh ? "智聊 ChatX" : "ChatX",
    alternateName: zh ? ["ChatX", "智聊"] : ["智聊 ChatX"],
    url: `${siteUrl}${zh ? "" : "/en"}/download/chatx`,
    applicationCategory: "BusinessApplication",
    applicationSubCategory: zh ? "全渠道 AI 客服工作台" : "Omni-channel AI customer service workspace",
    operatingSystem: "Windows 10/11",
    softwareVersion: d.version,
    downloadUrl: `${siteUrl}${d.url}`,
    fileSize: d.size[lang],
    softwareRequirements: zh
      ? "Windows 10 / 11（64 位）；约 1.5 GB 可用磁盘；无需独立显卡（AI 推理在云端或内网节点完成）"
      : "Windows 10 / 11 (64-bit); ~1.5 GB free disk; no dedicated GPU required (AI inference runs on cloud or LAN nodes)",
    inLanguage: ["zh-CN", "en"],
    image: `${siteUrl}/brand/products/chatx.png`,
    isAccessibleForFree: true,
    featureList: zh
      ? [
          "全渠道统一收件箱（Telegram / WhatsApp / Messenger / LINE）",
          "AI 自动拟稿与自动回复",
          "实时互译（标准翻译永久免费不限量）",
          "克隆声语音消息",
          "客户画像与跟进提醒",
          "数据保存在本机用户目录",
          "内置自动更新，SHA-256 可校验",
        ]
      : [
          "Unified omni-channel inbox (Telegram / WhatsApp / Messenger / LINE)",
          "AI drafting and auto-reply",
          "Live two-way translation (standard translation free and unlimited)",
          "Cloned-voice voice messages",
          "Customer profiles and follow-up reminders",
          "Data stored in your local user folder",
          "Built-in auto-update, SHA-256 verifiable",
        ],
    offers: {
      "@type": "Offer",
      price: "0",
      priceCurrency: "USD",
      description: zh ? "免费下载，注册即用（非限时试用）" : "Free download, free to start (not a timed trial)",
    },
    publisher: { "@id": `${siteUrl}/#organization` },
  };

  const faq = faqPageJsonLd(
    CHATX.install.faqs.map((f) => ({ q: plainText(f.q[lang]), a: plainText(f.a[lang]) })),
  );
  return [app, faq];
}
