/**
 * 智控 MatrixX 产品内容单一数据源（介绍页 /matrix 与下载页 /matrix/download 共用）。
 *
 * 与幻境 STUDIO（原 AvatarHub）的差异（刻意区分，勿套用 manualContent.ts）：
 * - 智控是 Telegram 多账号矩阵运营工具：**不需要显卡**、Windows 全量安装包一次到位；
 * - 最大上手卡点不是硬件，而是 Telegram 官方 API_ID / API_HASH 的获取——下载页单独重点图解；
 * - 登录本地闭环（密码注册即用 / 掃码登录），数据不出本机。
 *
 * 发布新版本时：更新 download.version / size / filename / sha256 与 releaseNotes 头部即可。
 * sha256 必须与实际上传到 /releases/matrixx/ 的安装包一致（发布时 certutil -hashfile 计算后填入）。
 */
import type { BrandLang } from "./brand";

export interface Mx<T = string> {
  zh: T;
  en: T;
}

/** 下载源基址：与 electron-builder.yml 的 publish url 指向同一目录（同源，避免官网下载版与客户端自更新版不一致）。 */
export const MATRIXX_RELEASE_BASE = "/releases/matrixx";

export const MATRIXX = {
  hero: {
    kicker: { zh: "智连系 · GROWTH", en: "Growth family" },
    title: {
      zh: "Telegram 多账号矩阵化运营，规模化不失控",
      en: "Telegram fleet operations at scale — without losing control",
    },
    subtitle: {
      zh: "多账号统一调度、AI 团队 24 小时协作跟进，配合智能频控与本地私有部署，让规模化增长真正可持续、可衡量。",
      en: "Unified multi-account orchestration, 24/7 AI-team follow-ups, smart rate control and fully local deployment — sustainable, measurable growth at scale.",
    },
    trustline: {
      zh: "本地运行 · 数据不出网 · 免显卡 · 一次安装即用",
      en: "Runs locally · data stays on-device · no GPU · one install, ready to go",
    },
  },
  caps: [
    {
      title: { zh: "多账号矩阵管理", en: "Fleet account management" },
      desc: {
        zh: "几十上百个 Telegram 账号统一登录、健康监测、节律控制，规模化运营不再靠人工盯盘。",
        en: "Dozens to hundreds of Telegram accounts under one console — login, health checks and cadence control, no manual babysitting.",
      },
    },
    {
      title: { zh: "搜索发现 · 资源中心", en: "Search & discovery" },
      desc: {
        zh: "按关键词批量发现群组 / 频道 / 资源，一键加入并纳入监控，持续沉淀获客资产。",
        en: "Discover groups / channels / resources by keyword in bulk, join and monitor in one click, building a lasting lead asset.",
      },
    },
    {
      title: { zh: "群组监控 · 触发规则", en: "Group monitoring & triggers" },
      desc: {
        zh: "监控目标群消息，命中关键词自动触发提取、打招呼、转私域等动作，机会不漏接。",
        en: "Watch target groups; keyword hits auto-trigger extraction, greetings and funneling — never miss an opportunity.",
      },
    },
    {
      title: { zh: "成员提取 · 价值分级", en: "Member extraction & scoring" },
      desc: {
        zh: "批量提取群成员、识别在线活跃度与价值分级，沉淀为可运营的统一联系人库。",
        en: "Bulk-extract members, gauge activity and value tiers, and consolidate them into an operable contact base.",
      },
    },
    {
      title: { zh: "消息触达 · 智能频控", en: "Broadcast with smart rate control" },
      desc: {
        zh: "账号轮换、FloodWait 冷却、频率限制与行为模拟结合，按平台规则控制节奏、规模化触达。",
        en: "Account rotation, FloodWait cooldown, rate limiting and behavior simulation — reach at scale while pacing within platform rules.",
      },
    },
    {
      title: { zh: "AI 自动回复 · 知识大脑", en: "AI auto-reply with knowledge base" },
      desc: {
        zh: "基于 RAG 知识库的拟人 AI 自动接客、答疑、跟进促单，人工可随时一键接管。",
        en: "RAG-powered human-like AI greets, answers, follows up and closes — hand off to a human anytime.",
      },
    },
  ],
  scenarios: {
    title: { zh: "谁在用智控 MatrixX", en: "Who uses MatrixX" },
    items: [
      { zh: "出海电商与私域运营团队", en: "Cross-border e-commerce & private-domain teams" },
      { zh: "跨境营销与获客团队", en: "Cross-border marketing & lead-gen teams" },
      { zh: "代理商 / 渠道商多号矩阵", en: "Agencies & resellers running account fleets" },
      { zh: "需要在 Telegram 生态批量触达与培育客户的团队", en: "Teams reaching & nurturing customers across Telegram at scale" },
    ],
  },
  /** 下载构建信息（发布时同步）。 */
  download: {
    version: "2.1.1",
    size: { zh: "507 MB", en: "507 MB" },
    filename: "MatrixX-2.1.1-Setup.exe",
    os: { zh: "Windows 10 / 11（64 位）", en: "Windows 10 / 11 (x64)" },
    /** 相对下载源基址；最终 URL = MATRIXX_RELEASE_BASE + "/" + filename。 */
    url: `${MATRIXX_RELEASE_BASE}/MatrixX-2.1.1-Setup.exe`,
    /** 发布时用 certutil -hashfile <exe> SHA256 计算后填入；留空则页面显示「发布时公布」。 */
    sha256: "82d1b37fc1acf0706bbc5f13d9c4afb8d5df557f2e6b075e5cfcc17c78e9ee7d",
    macNote: {
      zh: "macOS / Linux 版规划中：后端为 Windows 原生打包，跨平台需分别构建，暂未提供。",
      en: "macOS / Linux builds are planned: the backend is packaged natively for Windows; cross-platform builds are not available yet.",
    },
  },
  preCheck: [
    { zh: "系统：Windows 10 / 11（64 位）", en: "OS: Windows 10 / 11 (64-bit)" },
    { zh: "显卡：无需独立显卡（不做本地大模型推理）", en: "GPU: none required (no local heavy inference)" },
    { zh: "磁盘：预留约 2 GB 空间", en: "Disk: keep ~2 GB free" },
    { zh: "网络：需能正常访问 Telegram（海外网络环境）", en: "Network: normal access to Telegram (overseas connectivity)" },
    { zh: "凭据：一组 Telegram API_ID / API_HASH（下方第 4 步图解获取）", en: "Credentials: one Telegram API_ID / API_HASH (see step 4 below)" },
  ],
  /** 安装 / 上手步骤：富文本标记见 components/RichText.tsx（**强调** / [[术语]]）。 */
  steps: [
    {
      title: { zh: "下载安装包", en: "Download the installer" },
      time: { zh: "约 1–3 分钟", en: "~1–3 min" },
      detail: {
        zh: "在本页点击下载按钮，获取 **MatrixX-2.1.1-Setup.exe**（约 507 MB 全量包，含前端与本地后端，无需再下组件）。",
        en: "Click the download button on this page to get **MatrixX-2.1.1-Setup.exe** (~507 MB, full package with front-end and local backend — no extra components to download).",
      },
    },
    {
      title: { zh: "运行安装程序", en: "Run the installer" },
      time: { zh: "约 1 分钟", en: "~1 min" },
      detail: {
        zh: "双击安装包，一路点 **下一步**，可自选安装目录。安装完成后桌面与开始菜单会出现「智控 MatrixX」图标。",
        en: "Double-click the installer, click **Next** through the wizard, choose an install folder if you like. A MatrixX icon appears on the desktop and Start menu.",
      },
      warn: {
        zh: "最常见卡点：**[[SmartScreen]]** 提示「已保护你的电脑」——这是 Windows 对新发布程序的例行提醒，不是病毒报警。点 **更多信息 → 仍要运行** 即可。",
        en: "Most common bump: **[[SmartScreen]]** shows \"Windows protected your PC\" — a routine notice for newly published apps, not a virus alert. Click **More info → Run anyway**.",
      },
    },
    {
      title: { zh: "注册并登录", en: "Register and sign in" },
      time: { zh: "约 1 分钟", en: "~1 min" },
      detail: {
        zh: "首次打开后在登录页 **注册** 一个本地账号（邮箱 + 密码）即可直接进入——**全程本地、无需付费、数据不出本机**。也支持 Telegram 掃码登录。",
        en: "On first launch, **register** a local account (email + password) on the login page and you're in — **fully local, no payment, data stays on your machine**. Telegram QR login is also supported.",
      },
    },
    {
      title: { zh: "填入 Telegram API 凭据（关键一步）", en: "Enter your Telegram API credentials (key step)" },
      time: { zh: "约 3–5 分钟", en: "~3–5 min" },
      detail: {
        zh: "登录 **my.telegram.org** → **API development tools** → 填 App title/short name 创建应用 → 复制得到的 **api_id** 与 **api_hash**，回到智控「API 凭据」页粘贴保存。这组凭据用于连接你的 Telegram 账号，一次配置长期有效。",
        en: "Go to **my.telegram.org** → **API development tools** → create an app (any App title / short name) → copy the **api_id** and **api_hash**, then paste them into MatrixX's \"API credentials\" page. These connect your Telegram accounts and only need to be set once.",
      },
      sub: {
        zh: [
          "用你自己的 Telegram 手机号登录 my.telegram.org 获取验证码。",
          "一组 api_id / api_hash 可复用于该账号名下的多个登录会话。",
          "凭据只保存在本机，不上传任何服务器。",
        ],
        en: [
          "Sign in to my.telegram.org with your own Telegram phone number to get the code.",
          "One api_id / api_hash can serve multiple sessions under that account.",
          "Credentials are stored only on your machine, never uploaded.",
        ],
      },
    },
    {
      title: { zh: "添加 Telegram 账号", en: "Add your Telegram accounts" },
      time: { zh: "每个约 1 分钟", en: "~1 min each" },
      detail: {
        zh: "在「账号管理」添加你要运营的 Telegram 账号（掃码 / 验证码登录）。多个账号可组成矩阵，由智控统一调度。",
        en: "In \"Accounts\", add the Telegram accounts you'll operate (QR / code login). Multiple accounts form a fleet orchestrated by MatrixX.",
      },
    },
    {
      title: { zh: "开始运营", en: "Start operating" },
      time: { zh: "即刻", en: "Instant" },
      detail: {
        zh: "搜索发现群组 → 监控 / 提取成员 → 配置 AI 自动回复与群发任务 → 用数据看板衡量转化。至此，你的第一个 Telegram 运营矩阵已经跑起来。",
        en: "Discover groups → monitor / extract members → set up AI auto-reply and broadcast tasks → measure conversion on the dashboard. Your first Telegram operation fleet is now live.",
      },
    },
  ],
  faqs: [
    {
      q: { zh: "SmartScreen / 杀毒软件拦截安装包怎么办？", en: "SmartScreen / antivirus blocks the installer — what do I do?" },
      a: {
        zh: "这是 Windows 对新发布、未做代码签名程序的例行提醒，不是病毒。点 **更多信息 → 仍要运行**；不放心可先核对本页 SHA-256 再放行。",
        en: "It's a routine warning for newly published, unsigned apps — not a virus. Click **More info → Run anyway**; if unsure, verify the SHA-256 on this page first.",
      },
    },
    {
      q: { zh: "Telegram API_ID / API_HASH 在哪里申请？", en: "Where do I get the Telegram API_ID / API_HASH?" },
      a: {
        zh: "登录 **my.telegram.org**，进入 **API development tools**，创建一个应用即可看到 api_id 与 api_hash。免费、几分钟完成，一次申请长期可用。详见上方第 4 步。",
        en: "Sign in to **my.telegram.org**, open **API development tools** and create an app to see your api_id and api_hash. Free, takes minutes, reusable long-term. See step 4 above.",
      },
    },
    {
      q: { zh: "密码登录和掃码登录怎么选？", en: "Password login vs QR login — which one?" },
      a: {
        zh: "自己用：**密码注册登录最省事**，不依赖外网与机器人。想用 Telegram 身份直接进：用掃码登录。两者都在本机闭环完成。",
        en: "For personal use, **password login is simplest** — no external services needed. To sign in with your Telegram identity, use QR login. Both complete locally.",
      },
    },
    {
      q: { zh: "账号被 Telegram 限制（FloodWait）了怎么办？", en: "My account hit a Telegram limit (FloodWait) — now what?" },
      a: {
        zh: "智控内置账号轮换、FloodWait 冷却与频率控制。建议新号先「养号」、控制单账号动作频率、配置代理池，降低风控风险。",
        en: "MatrixX has built-in rotation, FloodWait cooldown and rate control. Warm up new accounts, keep per-account action rates low and configure a proxy pool to reduce risk.",
      },
    },
    {
      q: { zh: "数据保存在哪里？会上传吗？", en: "Where is my data stored? Is it uploaded?" },
      a: {
        zh: "账号、联系人、消息等数据全部保存在本机（用户数据目录），**不上传公网**。卸载不勾选删除数据则保留。",
        en: "Accounts, contacts and messages are stored locally (user-data folder) and **not uploaded**. They persist unless you choose to remove data on uninstall.",
      },
    },
    {
      q: { zh: "怎么更新到新版本？", en: "How do I update to a new version?" },
      a: {
        zh: "客户端内置自动更新：启动后自动检测新版本、后台下载，退出时自动安装，账号与数据全部保留，无需手动重装。",
        en: "The client has built-in auto-update: it checks on launch, downloads in the background and installs on exit — accounts and data preserved, no manual reinstall.",
      },
    },
  ],
} as const;

/** 便捷取值：按语言取一段文案。 */
export function mx(field: Mx, lang: BrandLang): string {
  return field[lang];
}
